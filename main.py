from __future__ import annotations

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from . import api as kgapi
from . import cards as cardlib
from .api import ApiError
from .delivery import deliver_song
from .quality import QUALITY_LABEL, trial_label

PLUGIN_DIR = str(Path(__file__).resolve().parent)


def _is_plugin_command_msg(msg: str) -> bool:
    return bool(
        re.match(
            r"^#?(?:kg|KG)(?:[^\x00-\x7F]|\b)|^#?\s*听\s*[1-9]|^#?(kg|KG)听\s*[1-9]",
            str(msg or "").strip(),
            re.IGNORECASE,
        )
    )


def _is_kg_message(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"kugou\.com|kugou\.net", text, re.IGNORECASE))


def _collect_message_text(event: AstrMessageEvent) -> str:
    parts: list[str] = []
    try:
        msg_str = event.message_str
        if msg_str:
            parts.append(str(msg_str))
    except Exception:
        pass
    try:
        mobj = event.message_obj
        chain = getattr(mobj, "message", None) or []
        from astrbot.api.message_components import Plain as _Plain

        for seg in chain:
            if isinstance(seg, _Plain):
                t = seg.text if hasattr(seg, "text") else None
                if t:
                    parts.append(str(t))
    except Exception:
        pass
    try:
        raw = event.message_obj.raw_message
        if raw:
            if isinstance(raw, str):
                parts.append(raw)
            else:
                parts.append(json.dumps(raw, ensure_ascii=False, default=str))
    except Exception:
        pass
    return "\n".join(p for p in parts if p)


class KugouMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 注入配置访问器给 api 模块
        kgapi.set_config_getter(lambda: self.config or {})
        self._active_logins: dict = {}

    # ──────────── 生命周期 ────────────

    async def initialize(self):
        """启动时确保设备 dfid（酷狗取链/搜索需要），持久化在 plugin_data 下避免重复注册。"""
        try:
            await self._ensure_device()
        except Exception as e:
            self._log_warn(f"初始化设备 Cookie 失败: {e}")

    async def _ensure_device(self):
        from astrbot.api.star import StarTools

        data_dir = StarTools.get_data_dir()
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        dev_file = data_dir / "device_cookies.json"
        try:
            if dev_file.exists():
                raw = json.loads(dev_file.read_text(encoding="utf-8"))
                if raw.get("cookie"):
                    kgapi.set_device_cookie(str(raw["cookie"]))
                    return
        except Exception:
            pass
        dfid = await kgapi.register_dev()
        if dfid:
            try:
                dev_file.write_text(
                    json.dumps({"cookie": f"dfid={dfid}", "ts": int(time.time())}, ensure_ascii=False),
                    encoding="utf-8",
                )
                self._log_info(f"已注册酷狗设备 dfid（{dfid[:6]}…）")
            except Exception as e:
                self._log_warn(f"持久化设备 Cookie 失败: {e}")

    # ──────────── 辅助 ────────────

    def _cfg(self) -> dict:
        return self.config or {}

    def _log_warn(self, msg: str):
        logger.warning(f"[kugou] {msg}")

    def _log_info(self, msg: str):
        logger.info(f"[kugou] {msg}")

    def _plain(self, text: str) -> Plain:
        return Plain(text=text)

    async def _send_chain(self, event: AstrMessageEvent, *components):
        comps = [c for c in components if c is not None]
        if not comps:
            return
        mc = MessageChain(chain=list(comps))
        mc.use_markdown_ = False
        try:
            await event.send(mc)
        except AttributeError:
            import traceback as _tb

            self._log_warn(f"_send_chain 发送失败（AttributeError）:\n{_tb.format_exc()}")
            texts = []
            for _c in comps:
                t = getattr(_c, "text", None)
                if t:
                    texts.append(str(t))
            if texts:
                try:
                    _fb = MessageChain(chain=[self._plain("\n".join(texts))])
                    _fb.use_markdown_ = False
                    await event.send(_fb)
                except Exception as _e2:
                    self._log_warn(f"_send_chain 文本兜底也失败: {_e2}")

    async def _reply(self, event: AstrMessageEvent, text: str):
        try:
            await self._send_chain(event, self._plain(text))
        except Exception as e:
            import traceback as _tb

            self._log_warn(f"_reply 发送失败: {e}\n{_tb.format_exc()}")

    def _scope(self, event: AstrMessageEvent) -> str:
        gid = getattr(event.message_obj, "group_id", None)
        if gid:
            return str(gid)
        return event.get_sender_id()

    def _user_key(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "")

    def _cmd(self, event: AstrMessageEvent, pattern: str, *, song_request: bool = False) -> re.Match | None:
        cfg = self._cfg()
        if not cfg.get("enable", True):
            return None
        if song_request and cfg.get("enableSongRequest") is False:
            return None
        return re.match(pattern, event.message_str.strip(), re.IGNORECASE)

    # ──────────── 关键词 → 资源解析 ────────────

    async def _resolve_song(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        # 32 位 hex = 酷狗文件 hash，直接查详情
        if re.fullmatch(r"[0-9a-fA-F]{32}", kw):
            try:
                s = await kgapi.audio_by_hash(kw.upper())
            except ApiError:
                s = None
            if s:
                return s
        lst = await kgapi.search(kw, "song", pagesize=1)
        return lst[0] if lst else None

    async def _resolve_playlist(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        if re.fullmatch(r"\d+", kw):
            return {"id": kw, "name": "", "cover": "", "songCount": 0, "creator": ""}
        pls = await kgapi.search(kw, "special", pagesize=5)
        return pls[0] if pls else None

    async def _resolve_album(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        if re.fullmatch(r"\d+", kw):
            return {"id": kw, "name": "", "cover": "", "artist": ""}
        albums = await kgapi.search(kw, "album", pagesize=5)
        return albums[0] if albums else None

    async def _resolve_artist(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        if re.fullmatch(r"\d+", kw):
            return {"id": kw, "name": "", "cover": ""}
        artists = await kgapi.search(kw, "author", pagesize=5)
        return artists[0] if artists else None

    # ──────────── 登录态 ────────────

    def _has_cookie(self) -> bool:
        return bool(self._cfg().get("defaultCookie"))

    async def _require_login(self, event: AstrMessageEvent) -> bool:
        """未登录时提示并消费事件，返回 False（调用方需立即 return）。"""
        if not self._has_cookie():
            await self._reply(event, "需要登录后使用，请先 #kg登录")
            event.stop_event()
            return False
        return True

    # ──────────── 取链 ────────────

    async def _resolve_play(self, song: dict, cfg: dict) -> dict:
        trial = cfg.get("trialFallback", True) is not False
        try:
            play = await kgapi.song_url_best(song, cfg.get("quality") or "auto", trial_fallback=trial)
            q = play.get("quality") or ""
            return {
                "url": play.get("url", ""),
                "quality": q,
                "qualityLabel": QUALITY_LABEL.get(q, q),
                "trial": bool(play.get("trial")),
                "raw": play.get("raw"),
            }
        except ApiError as e:
            return {"url": "", "error": str(e), "raw": getattr(e, "payload", None)}

    async def _play_song(self, event: AstrMessageEvent, song: dict, *, source: str = "") -> None:
        cfg = self._cfg()
        play = await self._resolve_play(song, cfg)
        quality_label = trial_label(play)
        if play.get("url"):
            tip = "正在下载并发送语音/文件…"
        elif play.get("error"):
            tip = play["error"]
        else:
            tip = "⚠ 未获取到播放链接，可尝试 #kg登录 或换个音质"
        data = cardlib.build_detail_card_data(song, quality_label, source=source, tip=tip)
        await self._reply_card_or_text(
            event,
            tpl_name="kg-detail",
            data=data,
            format_text=lambda d: cardlib.format_detail_text(song, play, tip),
        )
        if play.get("url"):
            await deliver_song(self, event, song, play, cfg=cfg, plugin_dir=PLUGIN_DIR)
            self._report_play_history(song)

    def _report_play_history(self, song: dict):
        """播放后自动上报听歌历史（需登录；fire-and-forget，失败不打扰）。"""
        if not self._has_cookie():
            return
        mxid = song.get("mixsongid") or song.get("id") or ""
        if not mxid:
            return

        async def _do():
            try:
                await kgapi.playhistory_upload(mxid)
            except ApiError as e:
                self._log_warn(f"上报听歌历史失败: {e}")

        try:
            asyncio.create_task(_do())
        except Exception:
            pass

    async def _list_to_session(self, event: AstrMessageEvent, keyword: str, songs: list, *, tip: str = "") -> bool:
        scope = self._scope(event)
        await cardlib.SessionStore.set(self, scope, {"type": "kg_songs", "keyword": keyword, "data": songs})
        text = cardlib.format_song_list(songs, keyword, tip=tip)
        if self._cfg().get("renderListCard", True):
            data = cardlib.build_list_card_data(keyword, songs, options={"tip": tip}, cfg=self._cfg())
            if await self._reply_card_or_text(event, tpl_name="kg-list", data=data, format_text=lambda d: text):
                return True
        await self._reply(event, text)
        return True

    # ──────────── 卡片渲染 ────────────

    async def _render_card(self, event: AstrMessageEvent, data: dict, tpl_name: str) -> str | None:
        try:
            import jinja2
            from playwright.async_api import async_playwright

            from .tpl_adapter import get_jinja_template

            tmpl_path = os.path.join(PLUGIN_DIR, "resources", "html", tpl_name, f"{tpl_name}.html")
            if not os.path.exists(tmpl_path):
                return None
            tmpl = get_jinja_template(tmpl_path)
            html = jinja2.Template(tmpl).render(data=data)
            async with async_playwright() as p:
                launch_args = [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
                try:
                    browser = await p.chromium.launch(args=launch_args)
                except Exception as e:
                    err_msg = str(e)
                    if "Executable doesn't exist" in err_msg or "playwright install" in err_msg:
                        logger.warning("[kugoumusic] 未找到 Playwright Chromium，正在尝试通过 npmmirror 镜像源自动下载安装...")
                        def _install():
                            cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
                            env = os.environ.copy()
                            env["PLAYWRIGHT_DOWNLOAD_HOST"] = "https://npmmirror.com/mirrors/playwright/"
                            subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
                        await asyncio.to_thread(_install)
                        browser = await p.chromium.launch(args=launch_args)
                    else:
                        raise e
                try:
                    page = await browser.new_page(
                        viewport={"width": 640, "height": 800},
                        device_scale_factor=2,  # 2x 清晰度
                    )
                    await page.set_content(html, wait_until="load", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    try:
                        rect = await page.evaluate(
                            "() => { const el = document.querySelector('.page') || document.body; "
                            "const r = el.getBoundingClientRect(); "
                            "return { w: Math.max(1, Math.ceil(r.right)), "
                            "h: Math.max(1, Math.ceil(r.bottom)) }; }"
                        )
                        await page.set_viewport_size({"width": rect["w"], "height": rect["h"]})
                        await page.wait_for_timeout(50)
                    except Exception:
                        pass
                    raw = await page.screenshot(full_page=True, type="png")
                finally:
                    await browser.close()
            from .delivery import get_temp_dir

            d = get_temp_dir(self._cfg(), PLUGIN_DIR)
            file_path = os.path.join(d, f"card_{tpl_name}_{int(time.time() * 1000)}.png")
            with open(file_path, "wb") as f:
                f.write(raw)
            return file_path
        except Exception as e:
            self._log_warn(f"{tpl_name} 本地渲染失败: {e}")
            return None

    async def _reply_card_or_text(self, event: AstrMessageEvent, *, tpl_name: str, data: dict, format_text) -> bool:
        card_path = None
        try:
            card_path = await self._render_card(event, data, tpl_name)
            if card_path:
                await self._send_chain(event, Image.fromFileSystem(card_path))
                return True
        except Exception as e:
            self._log_warn(f"{tpl_name} 卡片渲染失败，回退文本: {e}")
        finally:
            if card_path:
                asyncio.get_running_loop().call_later(
                    max(0, int(self._cfg().get("keepFileSec", 60))),
                    lambda: self._safe_unlink(card_path),
                )
        try:
            text = format_text(data)
            if text:
                await self._send_chain(event, self._plain(text))
                return True
        except Exception as e:
            self._log_warn(f"{tpl_name} 文本兜底失败: {e}")
        return False

    async def _save_qr_image(self, b64: str) -> str | None:
        try:
            import base64

            raw = b64
            if "," in raw and raw.split(",", 1)[0].startswith("data:"):
                raw = raw.split(",", 1)[1]
            data = base64.b64decode(raw)
            from .delivery import get_temp_dir

            path = os.path.join(get_temp_dir(self._cfg(), PLUGIN_DIR), f"qr_{int(time.time() * 1000)}.png")
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception as e:
            self._log_warn(f"保存二维码失败: {e}")
            return None

    def _safe_unlink(self, path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    # ══════════════════ 点歌 ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*点歌\s*(.+)$", re.IGNORECASE))
    async def pick_song(self, event: AstrMessageEvent):
        """#kg点歌 关键词：搜索并列出歌曲列表，发送 #kg听序号 播放"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*点歌\s*(.+)$", song_request=True)
        if not m:
            return
        keyword = m.group(1).strip()
        if not keyword:
            await self._reply(event, "用法：#kg点歌 关键词")
            event.stop_event()
            return
        try:
            await self._reply(event, f"正在搜索：{keyword}")
            page_size = min(int(self._cfg().get("maxList") or 10), 20)
            lst = await kgapi.search(keyword, "song", pagesize=page_size)
            if not lst:
                await self._reply(event, "没有搜到相关歌曲")
                event.stop_event()
                return
            await self._list_to_session(event, keyword, lst)
        except ApiError as err:
            self._log_warn(f"点歌失败: {err}")
            await self._reply(event, f"点歌失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*听\s*([1-9][0-9]?)$|^#?\s*听\s*([1-9][0-9]?)$", re.IGNORECASE))
    async def choose_song(self, event: AstrMessageEvent):
        """#kg听N：播放当前酷狗点歌列表第 N 首（可只发 #听N）"""
        cfg = self._cfg()
        if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
            return
        m = re.match(
            r"^#?(?:kg|KG)\s*听\s*([1-9][0-9]?)$|^#?\s*听\s*([1-9][0-9]?)$", event.message_str.strip(), re.IGNORECASE
        )
        n = int(m.group(1) or m.group(2) or 0) if m else 0
        scope = self._scope(event)
        session = await cardlib.SessionStore.get(self, scope)
        # 会话必须是本插件（kg_songs），否则不抢其它插件的 #听
        if not session or session.get("type") != "kg_songs" or not session.get("data"):
            return
        songs = session.get("data") or []
        if n < 1 or n > len(songs):
            await self._reply(event, f"序号超出范围（1-{len(songs)}）")
            event.stop_event()
            return
        song = songs[n - 1]
        try:
            await self._play_song(event, song, source="点歌")
        except ApiError as err:
            self._log_warn(f"播放失败: {err}")
            await self._reply(event, f"播放失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*播放\s*(.+)$", re.IGNORECASE))
    async def play_direct(self, event: AstrMessageEvent):
        """#kg播放 关键词：搜索并直接播放第一首"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*播放\s*(.+)$", song_request=True)
        if not m:
            return
        keyword = m.group(1).strip()
        if not keyword:
            await self._reply(event, "用法：#kg播放 关键词")
            event.stop_event()
            return
        try:
            lst = await kgapi.search(keyword, "song", pagesize=1)
            if not lst:
                await self._reply(event, f"没有搜到「{keyword}」")
                event.stop_event()
                return
            await self._play_song(event, lst[0], source="搜索")
        except ApiError as err:
            self._log_warn(f"播放失败: {err}")
            await self._reply(event, f"播放失败：{err}")
        event.stop_event()

    async def _fetch_lyric(self, song: dict) -> list:
        """按 hash 搜索歌词候选并取第一条，返回纯文本行列表。"""
        candidates = await kgapi.lyric_search(hash_=song.get("hash") or "", album_audio_id=song.get("mixsongid") or "")
        if not candidates:
            return []
        cand = candidates[0]
        if not cand.get("id") or not cand.get("accesskey"):
            return []
        lr = await kgapi.lyric(cand["id"], cand["accesskey"], fmt="lrc")
        content = lr.get("decodeContent") or ""
        if not content:
            return []
        return self._extract_lyric_lines(content)

    @staticmethod
    def _extract_lyric_lines(lrc: str, max_lines: int = 36) -> list:
        def _strip_meta(lines):
            return [l for l in lines if not re.match(r"^\s*\[(ti|ar|al|by|offset|total):", l, re.IGNORECASE)]

        out = []
        for l in _strip_meta(lrc.splitlines()):
            t = re.sub(r"^\[[^\]]*\]", "", l).strip()
            if t:
                out.append(t)
            if len(out) >= max_lines:
                break
        return out

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*歌词\s*(.+)$", re.IGNORECASE))
    async def get_lyric(self, event: AstrMessageEvent):
        """#kg歌词 关键词|hash：获取歌词"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*歌词\s*(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        if not kw:
            await self._reply(event, "用法：#kg歌词 关键词 或 #kg歌词 歌曲hash")
            event.stop_event()
            return
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            lines = await self._fetch_lyric(song)
            if not lines:
                await self._reply(event, "该歌曲暂无歌词")
                event.stop_event()
                return
            data = cardlib.build_lyric_card_data(song, lines, line_count=len(lines))
            await self._reply_card_or_text(
                event, tpl_name="kg-lyric", data=data, format_text=lambda d: cardlib.format_lyric_text(song, lines)
            )
        except ApiError as err:
            self._log_warn(f"歌词失败: {err}")
            await self._reply(event, f"获取歌词失败：{err}")
        event.stop_event()

    async def _fetch_krc_lyric(self, song: dict) -> list:
        """按 hash 取 KRC 逐字歌词，剥离 [时间] 与 <逐字> 标签返回文本行。"""
        candidates = await kgapi.lyric_search(hash_=song.get("hash") or "", album_audio_id=song.get("mixsongid") or "")
        if not candidates:
            return []
        cand = candidates[0]
        if not cand.get("id") or not cand.get("accesskey"):
            return []
        lr = await kgapi.lyric(cand["id"], cand["accesskey"], fmt="krc")
        content = lr.get("decodeContent") or ""
        if not content:
            return []
        out = []
        for l in content.splitlines():
            t = re.sub(r"^\[[^\]]*\]", "", l)
            t = re.sub(r"<[^>]*>", "", t).strip()  # KRC 逐字标签
            if t:
                out.append(t)
            if len(out) >= 36:
                break
        return out

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*逐字歌词\s+(.+)$", re.IGNORECASE))
    async def lyric_word(self, event: AstrMessageEvent):
        """#kg逐字歌词 关键词|hash：KRC 逐字歌词"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*逐字歌词\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            lines = await self._fetch_krc_lyric(song)
            if not lines:
                await self._reply(event, "该歌曲暂无逐字歌词")
                event.stop_event()
                return
            data = cardlib.build_lyric_card_data(song, lines, line_count=len(lines))
            data["tip"] = "逐字歌词来自酷狗音乐"
            await self._reply_card_or_text(
                event, tpl_name="kg-lyric", data=data, format_text=lambda d: cardlib.format_lyric_text(song, lines)
            )
        except ApiError as err:
            self._log_warn(f"逐字歌词失败: {err}")
            await self._reply(event, f"获取逐字歌词失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*热搜$", re.IGNORECASE))
    async def hot_search(self, event: AstrMessageEvent):
        """#kg热搜：酷狗热搜榜"""
        if not self._cfg().get("enable", True):
            return
        try:
            items = await kgapi.hot_search()
            if not items:
                await self._reply(event, "暂无热搜数据")
                event.stop_event()
                return
            data = cardlib.build_hot_card_data(items)
            await self._reply_card_or_text(
                event, tpl_name="kg-hot", data=data, format_text=lambda d: cardlib.format_hot_text(items)
            )
        except ApiError as err:
            self._log_warn(f"热搜失败: {err}")
            await self._reply(event, f"获取热搜失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*(help|帮助|菜单)$", re.IGNORECASE))
    async def help(self, event: AstrMessageEvent):
        """#kg帮助：查看全部指令"""
        if not self._cfg().get("enable", True):
            return
        try:
            version = "?"
            try:
                import yaml

                with open(os.path.join(PLUGIN_DIR, "metadata.yaml"), "r", encoding="utf-8") as f:
                    _meta = yaml.safe_load(f) or {}
                version = str(_meta.get("version", "?")).lstrip("v")
            except Exception:
                pass
            data = cardlib.build_help_card_data(version, self._cfg())
            await self._reply_card_or_text(
                event,
                tpl_name="kg-help",
                data=data,
                format_text=lambda d: cardlib.format_help_text(self._cfg(), version),
            )
        except Exception as err:
            self._log_warn(f"帮助失败: {err}")
            await self._reply(event, cardlib.format_help_text(self._cfg()))
        event.stop_event()

    # ══════════════════ 探索 ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*排行\s*(.*)$", re.IGNORECASE))
    async def chart(self, event: AstrMessageEvent):
        """#kg排行 [榜单名]：查看排行榜列表或具体榜单歌曲"""
        if not self._cfg().get("enable", True):
            return
        m = re.match(r"^#?(?:kg|KG)\s*排行\s*(.*)$", event.message_str.strip(), re.IGNORECASE)
        name = (m.group(1).strip() if m else "").strip()
        try:
            tops = await kgapi.rank_list()
            if not tops:
                await self._reply(event, "暂无榜单数据")
                event.stop_event()
                return
            if not name:
                # 榜单太多，展示前 30 个，完整列表仍支持名称匹配
                shown = tops[:30]
                items = [
                    {"name": t["name"], "sub": f"更新 {t.get('update')}s" if t.get("update") else ""} for t in shown
                ]
                data = cardlib.build_generic_card_data(
                    "酷狗排行榜",
                    items,
                    subtitle=f"共 {len(tops)} 个榜单，显示前 {len(shown)} 个",
                    tip="发送 #kg排行 榜单名 查看（如 #kg排行 TOP500）",
                    cfg=self._cfg(),
                )
                await self._reply_card_or_text(
                    event,
                    tpl_name="kg-generic",
                    data=data,
                    format_text=lambda d: cardlib.format_generic_text(
                        "酷狗排行榜", items, tip="发送 #kg排行 榜单名 查看（如 #kg排行 TOP500）"
                    ),
                )
                event.stop_event()
                return
            target = None
            for t in tops:
                if name == str(t["id"]) or name in t["name"] or t["name"] in name:
                    target = t
                    break
            if not target:
                await self._reply(event, f"未找到榜单「{name}」，发送 #kg排行 查看全部榜单")
                event.stop_event()
                return
            songs = await kgapi.rank_audio(target["id"], pagesize=60)
            if not songs:
                await self._reply(event, f"榜单「{target['name']}」暂无数据")
                event.stop_event()
                return
            await self._list_to_session(event, f"排行榜 · {target['name']}", songs)
        except ApiError as err:
            self._log_warn(f"排行失败: {err}")
            await self._reply(event, f"获取排行榜失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*歌手\s+(.+)$", re.IGNORECASE))
    async def artist(self, event: AstrMessageEvent):
        """#kg歌手 关键词：查看歌手热门歌曲"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*歌手\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            a = await self._resolve_artist(kw)
            if not a:
                await self._reply(event, f"没有搜到歌手「{kw}」")
                event.stop_event()
                return
            songs = await kgapi.artist_audios(a["id"], sort="hot", pagesize=30)
            if not songs:
                await self._reply(event, f"歌手「{a['name']}」暂无热门歌曲")
                event.stop_event()
                return
            await self._list_to_session(event, f"歌手 · {a['name']}", songs)
        except ApiError as err:
            self._log_warn(f"歌手失败: {err}")
            await self._reply(event, f"获取歌手歌曲失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*专辑\s+(.+)$", re.IGNORECASE))
    async def album(self, event: AstrMessageEvent):
        """#kg专辑 关键词：查看专辑曲目"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*专辑\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            a = await self._resolve_album(kw)
            if not a:
                await self._reply(event, f"没有搜到专辑「{kw}」")
                event.stop_event()
                return
            songs = await kgapi.album_songs(a["id"], pagesize=30)
            if not songs:
                await self._reply(event, f"专辑「{a['name']}」暂无曲目")
                event.stop_event()
                return
            await self._list_to_session(
                event, f"专辑 · {a['name']}", songs, tip=f"歌手：{a.get('artist') or ''} · 共 {len(songs)} 首"
            )
        except ApiError as err:
            self._log_warn(f"专辑失败: {err}")
            await self._reply(event, f"获取专辑失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*歌单\s+(.+)$", re.IGNORECASE))
    async def playlist(self, event: AstrMessageEvent):
        """#kg歌单 关键词|id：搜索歌单并查看曲目（VIP 歌单需登录）"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*歌单\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            p = await self._resolve_playlist(kw)
            if not p:
                await self._reply(event, f"没有搜到歌单「{kw}」")
                event.stop_event()
                return
            try:
                songs = await kgapi.playlist_tracks(p["id"], pagesize=100)
            except ApiError as e:
                if e.code in (20010, 20017):
                    await self._reply(event, f"获取歌单「{p['name'] or kw}」曲目需要登录：{e}")
                    event.stop_event()
                    return
                raise
            if not songs:
                await self._reply(event, f"歌单「{p['name'] or kw}」暂无曲目或需要登录")
                event.stop_event()
                return
            shown = songs[:30]
            await self._list_to_session(
                event, f"歌单 · {p['name']}", shown, tip=f"歌单共 {len(songs)} 首，显示前 {len(shown)} 首"
            )
        except ApiError as err:
            self._log_warn(f"歌单失败: {err}")
            await self._reply(event, f"获取歌单失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*评论\s+(.+)$", re.IGNORECASE))
    async def get_comment(self, event: AstrMessageEvent):
        """#kg评论 关键词：获取歌曲热评"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*评论\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            mix = song.get("mixsongid") or song.get("id") or ""
            if not mix:
                await self._reply(event, "无法获取该歌曲评论（缺少歌曲 ID）")
                event.stop_event()
                return
            r = await kgapi.comment_music(mix, pagesize=20)
            if not r.get("comments"):
                await self._reply(event, "该歌曲暂无评论")
                event.stop_event()
                return
            data = cardlib.build_comment_card_data(song, r["comments"], total=r.get("count"))
            await self._reply_card_or_text(
                event,
                tpl_name="kg-comment",
                data=data,
                format_text=lambda d: cardlib.format_comment_text(song, r["comments"]),
            )
        except ApiError as err:
            self._log_warn(f"评论失败: {err}")
            await self._reply(event, f"获取评论失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*新歌\s*$", re.IGNORECASE))
    async def new_song(self, event: AstrMessageEvent):
        """#kg新歌：新歌速递"""
        if not self._cfg().get("enable", True):
            return
        try:
            songs = await kgapi.top_song(rank_id=21608, pagesize=30)
            if not songs:
                await self._reply(event, "暂无新歌数据")
                event.stop_event()
                return
            await self._list_to_session(event, "新歌速递", songs[:20])
        except ApiError as err:
            self._log_warn(f"新歌失败: {err}")
            await self._reply(event, f"获取新歌失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*精品歌单$", re.IGNORECASE))
    async def top_playlist(self, event: AstrMessageEvent):
        """#kg精品歌单：精选歌单"""
        if not self._cfg().get("enable", True):
            return
        try:
            pls = await kgapi.top_playlists(pagesize=15)
            if not pls:
                await self._reply(event, "暂无精品歌单数据")
                event.stop_event()
                return
            data = cardlib.build_playlist_card_data("精品歌单", pls, subtitle="精选歌单推荐", cfg=self._cfg())
            await self._reply_card_or_text(
                event,
                tpl_name="kg-playlist",
                data=data,
                format_text=lambda d: cardlib.format_playlist_text("精品歌单", pls),
            )
        except ApiError as err:
            self._log_warn(f"精品歌单失败: {err}")
            await self._reply(event, f"获取精品歌单失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*歌单分类$", re.IGNORECASE))
    async def catlist(self, event: AstrMessageEvent):
        """#kg歌单分类：歌单分类列表"""
        if not self._cfg().get("enable", True):
            return
        try:
            cats = await kgapi.playlist_tags()
            if not cats:
                await self._reply(event, "暂无歌单分类数据")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                "歌单分类",
                [
                    {"name": c["name"], "tag": f"{cardlib.fmt_count(c['count'])}" if c.get("count") else ""}
                    for c in cats[:40]
                ],
                subtitle="歌单标签分类",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    "歌单分类",
                    [
                        {"name": c["name"], "tag": f"{cardlib.fmt_count(c['count'])}" if c.get("count") else ""}
                        for c in cats[:40]
                    ],
                ),
            )
        except ApiError as err:
            self._log_warn(f"歌单分类失败: {err}")
            await self._reply(event, f"获取歌单分类失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*搜索建议\s+(.+)$", re.IGNORECASE))
    async def suggest(self, event: AstrMessageEvent):
        """#kg搜索建议 关键词：关键词补全"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*搜索建议\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            items = await kgapi.search_suggest(kw)
            if not items:
                await self._reply(event, "暂无补全建议")
            else:
                data = cardlib.build_generic_card_data(
                    f"「{kw}」的搜索建议",
                    [{"name": w} for w in items],
                    subtitle="关键词补全",
                    cfg=self._cfg(),
                )
                await self._reply_card_or_text(
                    event,
                    tpl_name="kg-generic",
                    data=data,
                    format_text=lambda d: cardlib.format_generic_text(
                        f"「{kw}」的搜索建议", [{"name": w} for w in items]
                    ),
                )
        except ApiError as err:
            self._log_warn(f"搜索建议失败: {err}")
            await self._reply(event, f"获取搜索建议失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*MV\s+(.+)$", re.IGNORECASE))
    async def mv(self, event: AstrMessageEvent):
        """#kgMV 关键词：查看 MV 详情与播放链接"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*MV\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            mvs = await kgapi.search(kw, "mv", pagesize=5)
            if not mvs:
                await self._reply(event, f"没有搜到「{kw}」的 MV")
                event.stop_event()
                return
            mv_item = mvs[0]
            url = await kgapi.video_url(mv_item.get("id") or "")
            lines = [
                f"🎬 MV：{mv_item.get('name') or ''} - {mv_item.get('artist') or ''}",
                f"时长：{mv_item.get('duration') or '未知'}",
            ]
            if url:
                lines.append(f"播放：{url}")
            else:
                lines.append("⚠ 未获取到 MV 播放地址（可能需登录）")
            await self._reply(event, "\n".join(lines))
        except ApiError as err:
            self._log_warn(f"MV 失败: {err}")
            await self._reply(event, f"获取 MV 失败：{err}")
        event.stop_event()

    # ══════════════════ 发现 · 扩展 ══════════════════

    NEW_ALBUM_AREAS = {"": 0, "推荐": 0, "华语": 1, "欧美": 2, "日本": 3, "韩国": 4}
    GOOD_SONG_CARDS = {"精选": 1, "怀旧": 2, "热门": 3, "小众": 4, "vip": 6, "VIP": 6}
    ARTIST_LIST_TYPES = {"": 0, "全部": 0, "华语": 1, "欧美": 2, "日韩": 3, "其他": 4, "日本": 5, "韩国": 6}

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*新碟\s*(.*)$", re.IGNORECASE))
    async def new_album(self, event: AstrMessageEvent):
        """#kg新碟 [华语/欧美/日本/韩国]：新碟上架"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*新碟\s*(.*)$")
        if not m:
            return
        area = m.group(1).strip()
        area_id = self.NEW_ALBUM_AREAS.get(area, 0)
        try:
            albums = await kgapi.top_album(area_id, pagesize=15)
            if not albums:
                await self._reply(event, "暂无新碟数据")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                f"新碟上架 · {area or '推荐'}",
                [
                    {
                        "name": a["name"],
                        "sub": a.get("artist") or "",
                        "tag": a.get("publishDate") or "",
                        "cover": a.get("cover") or "",
                    }
                    for a in albums
                ],
                subtitle="最新专辑",
                tip="发送 #kg专辑 专辑名 查看曲目",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    f"新碟上架 · {area or '推荐'}",
                    [{"name": a["name"], "sub": a.get("artist") or ""} for a in albums],
                    tip="发送 #kg专辑 专辑名 查看曲目",
                ),
            )
        except ApiError as err:
            self._log_warn(f"新碟失败: {err}")
            await self._reply(event, f"获取新碟失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*好歌\s*(.*)$", re.IGNORECASE))
    async def good_song(self, event: AstrMessageEvent):
        """#kg好歌 [精选/怀旧/热门/小众/VIP]：好歌精选卡片"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*好歌\s*(.*)$")
        if not m:
            return
        card = m.group(1).strip()
        card_id = self.GOOD_SONG_CARDS.get(card, 3)
        try:
            songs = await kgapi.top_card(card_id, pagesize=20)
            if not songs:
                await self._reply(event, "暂无推荐歌曲")
                event.stop_event()
                return
            await self._list_to_session(event, f"好歌精选 · {card or '热门'}", songs)
        except ApiError as err:
            self._log_warn(f"好歌失败: {err}")
            await self._reply(event, f"获取好歌失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*主题歌单\s*(.*)$", re.IGNORECASE))
    async def theme_playlist_cmd(self, event: AstrMessageEvent):
        """#kg主题歌单 [序号]：主题歌单列表 / 查看主题曲目"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*主题歌单\s*(.*)$")
        if not m:
            return
        arg = m.group(1).strip()
        scope = self._scope(event)
        session = await cardlib.SessionStore.get(self, scope)
        try:
            if re.fullmatch(r"\d+", arg) and session and session.get("type") == "kg_theme":
                n = int(arg)
                themes = session.get("data") or []
                if n < 1 or n > len(themes):
                    await self._reply(event, f"序号超出范围（1-{len(themes)}）")
                    event.stop_event()
                    return
                theme = themes[n - 1]
                songs = await kgapi.theme_playlist_tracks(theme["id"], pagesize=30)
                if not songs:
                    await self._reply(event, f"主题「{theme['name']}」暂无曲目")
                    event.stop_event()
                    return
                await self._list_to_session(event, f"主题 · {theme['name']}", songs)
                event.stop_event()
                return
            themes = await kgapi.theme_playlists(pagesize=20)
            if not themes:
                await self._reply(event, "暂无主题歌单")
                event.stop_event()
                return
            await cardlib.SessionStore.set(self, scope, {"type": "kg_theme", "data": themes})
            data = cardlib.build_generic_card_data(
                "主题歌单",
                [{"name": t["name"], "sub": t.get("intro") or "", "cover": t.get("cover") or ""} for t in themes],
                subtitle=f"共 {len(themes)} 个主题",
                tip="发送 #kg主题歌单 序号 查看曲目（如 #kg主题歌单 1）",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    "主题歌单",
                    [{"name": t["name"], "sub": t.get("intro") or ""} for t in themes],
                    tip="发送 #kg主题歌单 序号 查看曲目（如 #kg主题歌单 1）",
                ),
            )
        except ApiError as err:
            self._log_warn(f"主题歌单失败: {err}")
            await self._reply(event, f"获取主题歌单失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*乐库$", re.IGNORECASE))
    async def yueku_cmd(self, event: AstrMessageEvent):
        """#kg乐库：乐库各区块概览"""
        if not self._cfg().get("enable", True):
            return
        try:
            info = await kgapi.yueku()
            sections = info.get("sections") or {}
            labels = {
                "recommend": "推荐",
                "song": "新歌",
                "rank": "排行",
                "album": "专辑",
                "video": "视频",
                "topic": "专题",
            }
            items = [{"name": labels.get(k, k), "tag": f"{v} 条"} for k, v in sections.items() if v]
            if not items:
                await self._reply(event, "暂无乐库数据")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                "酷狗乐库",
                items,
                subtitle="乐库各区块",
                tip="发送 #kg好歌 / #kg新碟 / #kg排行 查看对应内容",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    "酷狗乐库", items, tip="发送 #kg好歌 / #kg新碟 / #kg排行 查看对应内容"
                ),
            )
        except ApiError as err:
            self._log_warn(f"乐库失败: {err}")
            await self._reply(event, f"获取乐库失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*编辑精选$", re.IGNORECASE))
    async def top_ip_cmd(self, event: AstrMessageEvent):
        """#kg编辑精选：编辑精选专题"""
        if not self._cfg().get("enable", True):
            return
        try:
            items = await kgapi.top_ip(pagesize=15)
            if not items:
                await self._reply(event, "暂无编辑精选数据")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                "编辑精选",
                [{"name": it["name"], "sub": it.get("sub") or "", "cover": it.get("cover") or ""} for it in items],
                subtitle="编辑精选专题",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text("编辑精选", [{"name": it["name"]} for it in items]),
            )
        except ApiError as err:
            self._log_warn(f"编辑精选失败: {err}")
            await self._reply(event, f"获取编辑精选失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*排行推荐$", re.IGNORECASE))
    async def rank_top_cmd(self, event: AstrMessageEvent):
        """#kg排行推荐：推荐的排行榜"""
        if not self._cfg().get("enable", True):
            return
        try:
            items = await kgapi.rank_top(pagesize=15)
            if not items:
                await self._reply(event, "暂无排行推荐数据")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                "推荐排行榜",
                [{"name": it["name"], "cover": it.get("cover") or ""} for it in items],
                subtitle="精选榜单",
                tip="发送 #kg排行 榜单名 查看歌曲",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    "推荐排行榜", [{"name": it["name"]} for it in items], tip="发送 #kg排行 榜单名 查看歌曲"
                ),
            )
        except ApiError as err:
            self._log_warn(f"排行推荐失败: {err}")
            await self._reply(event, f"获取排行推荐失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*历史日推\s*(.*)$", re.IGNORECASE))
    async def history_daily(self, event: AstrMessageEvent):
        """#kg历史日推 [序号]：历史每日推荐 / 查看某天歌曲"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*历史日推\s*(.*)$")
        if not m:
            return
        arg = m.group(1).strip()
        scope = self._scope(event)
        session = await cardlib.SessionStore.get(self, scope)
        try:
            if re.fullmatch(r"\d+", arg) and session and session.get("type") == "kg_history":
                n = int(arg)
                groups = session.get("data") or []
                if n < 1 or n > len(groups):
                    await self._reply(event, f"序号超出范围（1-{len(groups)}）")
                    event.stop_event()
                    return
                group = groups[n - 1]
                await self._list_to_session(event, f"历史日推 · {group['name']}", group["songs"])
                event.stop_event()
                return
            groups = await kgapi.everyday_history()
            if not groups:
                await self._reply(event, "暂无历史推荐记录")
                event.stop_event()
                return
            await cardlib.SessionStore.set(self, scope, {"type": "kg_history", "data": groups})
            data = cardlib.build_generic_card_data(
                "历史每日推荐",
                [{"name": g["name"], "tag": f"{g['count']} 首"} for g in groups],
                subtitle=f"共 {len(groups)} 期",
                tip="发送 #kg历史日推 序号 查看（如 #kg历史日推 1）",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    "历史每日推荐",
                    [{"name": g["name"], "tag": f"{g['count']} 首"} for g in groups],
                    tip="发送 #kg历史日推 序号 查看（如 #kg历史日推 1）",
                ),
            )
        except ApiError as err:
            self._log_warn(f"历史日推失败: {err}")
            await self._reply(event, f"获取历史日推失败：{err}")
        event.stop_event()

    # ══════════════════ 歌曲增强 ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*高潮\s+(.+)$", re.IGNORECASE))
    async def climax(self, event: AstrMessageEvent):
        """#kg高潮 关键词：歌曲高潮片段时间"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*高潮\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            c = await kgapi.song_climax(song.get("hash") or "")
            if not c.get("start_ms"):
                await self._reply(event, f"「{song['name']}」暂无高潮数据")
                event.stop_event()
                return
            fmt = lambda ms: f"{ms // 60000:02d}:{(ms % 60000) // 1000:02d}"
            await self._reply(
                event,
                f"🎯 高潮片段：{fmt(c['start_ms'])} - {fmt(c['end_ms'])}（约 {c['duration_ms'] // 1000} 秒）\n"
                f"♪ {song['name']} - {song['artist']}",
            )
        except ApiError as err:
            self._log_warn(f"高潮失败: {err}")
            await self._reply(event, f"获取高潮失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*(?:AI|ai)推荐\s+(.+)$", re.IGNORECASE))
    async def ai_recommend_cmd(self, event: AstrMessageEvent):
        """#kgAI推荐 关键词：AI 相似歌曲推荐"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*(?:AI|ai)推荐\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            mix = song.get("mixsongid") or song.get("id") or ""
            songs = await kgapi.ai_recommend(mix)
            if not songs:
                await self._reply(event, "暂无 AI 推荐歌曲")
                event.stop_event()
                return
            await self._list_to_session(event, f"AI 推荐 · {song['name']}", songs)
        except ApiError as err:
            self._log_warn(f"AI推荐失败: {err}")
            await self._reply(event, f"获取 AI 推荐失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*收藏\s+(.+)$", re.IGNORECASE))
    async def favorite_cmd(self, event: AstrMessageEvent):
        """#kg收藏 关键词：歌曲收藏数"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*收藏\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            mix = song.get("mixsongid") or song.get("id") or ""
            cnt = await kgapi.favorite_count(mix)
            await self._reply(
                event,
                f"⭐ 收藏数：{cnt or '未知'}\n♪ {song['name']} - {song['artist']}",
            )
        except ApiError as err:
            self._log_warn(f"收藏数失败: {err}")
            await self._reply(event, f"获取收藏数失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*(版本|相似)\s+(.+)$", re.IGNORECASE))
    async def song_versions(self, event: AstrMessageEvent):
        """#kg版本 关键词：同一首歌的其他版本（翻唱/remix等）"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*(?:版本|相似)\s+(.+)$")
        if not m:
            return
        kw = m.group(2).strip()
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            mix = song.get("mixsongid") or song.get("id") or ""
            songs = await kgapi.related_songs(mix)
            if not songs:
                await self._reply(event, "暂无其他版本")
                event.stop_event()
                return
            await self._list_to_session(event, f"更多版本 · {song['name']}", songs)
        except ApiError as err:
            self._log_warn(f"版本失败: {err}")
            await self._reply(event, f"获取更多版本失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*歌手专辑\s+(.+)$", re.IGNORECASE))
    async def artist_albums_cmd(self, event: AstrMessageEvent):
        """#kg歌手专辑 歌手：歌手的专辑列表"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*歌手专辑\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            a = await self._resolve_artist(kw)
            if not a:
                await self._reply(event, f"没有搜到歌手「{kw}」")
                event.stop_event()
                return
            albums = await kgapi.artist_albums(a["id"], pagesize=15)
            if not albums:
                await self._reply(event, f"歌手「{a['name']}」暂无专辑")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                f"专辑 · {a['name']}",
                [
                    {"name": al["name"], "sub": al.get("publishDate") or "", "cover": al.get("cover") or ""}
                    for al in albums
                ],
                subtitle=f"共 {len(albums)} 张专辑",
                tip="发送 #kg专辑 专辑名 查看曲目",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    f"专辑 · {a['name']}",
                    [{"name": al["name"], "sub": al.get("publishDate") or ""} for al in albums],
                    tip="发送 #kg专辑 专辑名 查看曲目",
                ),
            )
        except ApiError as err:
            self._log_warn(f"歌手专辑失败: {err}")
            await self._reply(event, f"获取歌手专辑失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*歌手列表\s*(.*)$", re.IGNORECASE))
    async def artist_list_cmd(self, event: AstrMessageEvent):
        """#kg歌手列表 [华语/欧美/日韩/日本/韩国]：歌手列表"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*歌手列表\s*(.*)$")
        if not m:
            return
        t = m.group(1).strip()
        type_ = self.ARTIST_LIST_TYPES.get(t, 0)
        try:
            artists = await kgapi.artist_lists(type_, hotsize=15)
            if not artists:
                await self._reply(event, "暂无歌手数据")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                f"歌手列表 · {t or '全部'}",
                [{"name": a["name"], "cover": a.get("cover") or ""} for a in artists],
                subtitle=f"共 {len(artists)} 位歌手",
                tip="发送 #kg歌手 歌手名 查看热门歌曲",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    f"歌手列表 · {t or '全部'}",
                    [{"name": a["name"]} for a in artists],
                    tip="发送 #kg歌手 歌手名 查看热门歌曲",
                ),
            )
        except ApiError as err:
            self._log_warn(f"歌手列表失败: {err}")
            await self._reply(event, f"获取歌手列表失败：{err}")
        event.stop_event()

    # ══════════════════ 评论扩展 ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*歌单评论\s+(.+)$", re.IGNORECASE))
    async def playlist_comment(self, event: AstrMessageEvent):
        """#kg歌单评论 关键词|id：歌单热评"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*歌单评论\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            p = await self._resolve_playlist(kw)
            if not p:
                await self._reply(event, f"没有搜到歌单「{kw}」")
                event.stop_event()
                return
            comments = await kgapi.comment_playlist(p["id"])
            if not comments:
                await self._reply(event, "该歌单暂无评论")
                event.stop_event()
                return
            data = cardlib.build_comment_card_data(p, comments, total=len(comments))
            await self._reply_card_or_text(
                event,
                tpl_name="kg-comment",
                data=data,
                format_text=lambda d: cardlib.format_comment_text(p, comments),
            )
        except ApiError as err:
            self._log_warn(f"歌单评论失败: {err}")
            await self._reply(event, f"获取歌单评论失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*专辑评论\s+(.+)$", re.IGNORECASE))
    async def album_comment(self, event: AstrMessageEvent):
        """#kg专辑评论 专辑：专辑热评"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*专辑评论\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            a = await self._resolve_album(kw)
            if not a:
                await self._reply(event, f"没有搜到专辑「{kw}」")
                event.stop_event()
                return
            comments = await kgapi.comment_album(a["id"])
            if not comments:
                await self._reply(event, "该专辑暂无评论")
                event.stop_event()
                return
            data = cardlib.build_comment_card_data(a, comments, total=len(comments))
            await self._reply_card_or_text(
                event,
                tpl_name="kg-comment",
                data=data,
                format_text=lambda d: cardlib.format_comment_text(a, comments),
            )
        except ApiError as err:
            self._log_warn(f"专辑评论失败: {err}")
            await self._reply(event, f"获取专辑评论失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*评论数\s+(.+)$", re.IGNORECASE))
    async def comment_count_cmd(self, event: AstrMessageEvent):
        """#kg评论数 关键词：歌曲评论数"""
        m = self._cmd(event, r"^#?(?:kg|KG)\s*评论数\s+(.+)$")
        if not m:
            return
        kw = m.group(1).strip()
        try:
            song = await self._resolve_song(kw)
            if not song:
                await self._reply(event, f"没有搜到「{kw}」")
                event.stop_event()
                return
            cnt = await kgapi.comment_count(song.get("hash") or "")
            await self._reply(
                event,
                f"💬 评论数：{cardlib.fmt_count(cnt) if cnt else '未知'}\n♪ {song['name']} - {song['artist']}",
            )
        except ApiError as err:
            self._log_warn(f"评论数失败: {err}")
            await self._reply(event, f"获取评论数失败：{err}")
        event.stop_event()

    # ══════════════════ 账号 · 扩展2（需登录） ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*云盘$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cloud(self, event: AstrMessageEvent):
        """#kg云盘：我的云盘歌曲（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        try:
            songs = await kgapi.user_cloud(pagesize=30)
            if not songs:
                await self._reply(event, "云盘暂无歌曲")
                event.stop_event()
                return
            await self._list_to_session(event, "我的云盘", songs[:20])
        except ApiError as err:
            self._log_warn(f"云盘失败: {err}")
            await self._reply(event, f"获取云盘失败：{err}\n需要先 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*已购$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def purchased(self, event: AstrMessageEvent):
        """#kg已购：已购单曲/专辑（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        try:
            songs = await kgapi.user_purchased_songs(pagesize=20)
            albums = await kgapi.user_purchased_albums(pagesize=10)
            lines = []
            if songs:
                lines.append(f"已购单曲（{len(songs)}）:")
                lines.extend(f"{i + 1}. {s['name']} - {s['artist']}" for i, s in enumerate(songs[:15]))
            if albums:
                lines.append(f"已购专辑（{len(albums)}）:")
                lines.extend(f"{i + 1}. {a['name']} - {a['artist']}" for i, a in enumerate(albums[:10]))
            if not lines:
                await self._reply(event, "暂无已购内容")
            else:
                await self._reply(event, "🎵 已购内容\n" + "\n".join(lines))
        except ApiError as err:
            self._log_warn(f"已购失败: {err}")
            await self._reply(event, f"获取已购失败：{err}\n需要先 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*等级$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def grade(self, event: AstrMessageEvent):
        """#kg等级：听歌等级（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        try:
            g = await kgapi.user_grade_info()
            if not g:
                await self._reply(event, "暂无听歌等级数据")
                event.stop_event()
                return
            hours = g["dSec"] / 3600
            lines = [
                f"🎧 听歌等级：Lv.{g['grade']}",
                f"累计听歌时长：{hours:.1f} 小时（{g['dSec']} 秒）",
                f"当前积分：{g['currentPoint']}",
            ]
            if g.get("nextGrade"):
                lines.append(f"距 Lv.{g['nextGrade']} 还差 {max(0, g['nextGradePoint'] - g['currentPoint'])} 分")
            await self._reply(event, "\n".join(lines))
        except ApiError as err:
            self._log_warn(f"等级失败: {err}")
            await self._reply(event, f"获取听歌等级失败：{err}\n需要先 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*(关注|取关|取消关注)\s+(.+)$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def follow_toggle(self, event: AstrMessageEvent):
        """#kg关注 歌手 / #kg取关 歌手：关注/取关歌手（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        m = re.match(r"^#?(?:kg|KG)\s*(关注|取关|取消关注)\s+(.+)$", event.message_str.strip(), re.IGNORECASE)
        action = m.group(1) if m else ""
        kw = (m.group(2).strip() if m else "").strip()
        try:
            a = await self._resolve_artist(kw)
            if not a:
                await self._reply(event, f"没有搜到歌手「{kw}」")
                event.stop_event()
                return
            if action in ("取关", "取消关注"):
                await kgapi.artist_unfollow(a["id"])
                await self._reply(event, f"已取消关注：{a['name']}")
            else:
                await kgapi.artist_follow(a["id"])
                await self._reply(event, f"已关注：{a['name']}")
        except ApiError as err:
            self._log_warn(f"关注操作失败: {err}")
            await self._reply(event, f"操作失败：{err}\n需要先 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*关注新歌$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def follow_newsongs(self, event: AstrMessageEvent):
        """#kg关注新歌：关注的歌手新歌（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        try:
            songs = await kgapi.artist_follow_newsongs(pagesize=30)
            if not songs:
                await self._reply(event, "暂无关注歌手新歌")
                event.stop_event()
                return
            await self._list_to_session(event, "关注歌手新歌", songs[:20])
        except ApiError as err:
            self._log_warn(f"关注新歌失败: {err}")
            await self._reply(event, f"获取关注新歌失败：{err}\n需要先 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*关注列表$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def follow_list(self, event: AstrMessageEvent):
        """#kg关注列表：我关注的歌手（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        try:
            artists = await kgapi.user_follow(pagesize=30)
            if not artists:
                await self._reply(event, "暂无关注歌手")
                event.stop_event()
                return
            data = cardlib.build_generic_card_data(
                "我关注的歌手",
                [{"name": a["name"], "cover": a.get("cover") or ""} for a in artists],
                subtitle=f"共 {len(artists)} 位",
                tip="发送 #kg歌手 歌手名 查看热门歌曲；#kg取关 歌手名 取关",
                cfg=self._cfg(),
            )
            await self._reply_card_or_text(
                event,
                tpl_name="kg-generic",
                data=data,
                format_text=lambda d: cardlib.format_generic_text(
                    "我关注的歌手",
                    [{"name": a["name"]} for a in artists],
                    tip="发送 #kg歌手 歌手名 查看热门歌曲；#kg取关 歌手名 取关",
                ),
            )
        except ApiError as err:
            self._log_warn(f"关注列表失败: {err}")
            await self._reply(event, f"获取关注列表失败：{err}\n需要先 #kg登录")
        event.stop_event()

    # ══════════════════ 推荐 ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*(推荐|日推|每日推荐)$", re.IGNORECASE))
    async def recommend(self, event: AstrMessageEvent):
        """#kg推荐 / #kg日推：每日推荐歌曲"""
        if not self._cfg().get("enable", True):
            return
        try:
            songs = await kgapi.everyday_recommend()
            if not songs:
                await self._reply(event, "今日暂无推荐")
                event.stop_event()
                return
            await self._list_to_session(event, "每日推荐", songs[:20])
        except ApiError as err:
            self._log_warn(f"日推失败: {err}")
            await self._reply(event, f"获取每日推荐失败：{err}\n可能需要 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*(来首歌|随机|放一首|来一首)$", re.IGNORECASE))
    async def random_song(self, event: AstrMessageEvent):
        """#kg来首歌：随机来一首（私人 FM 兜底每日推荐）"""
        if not self._cfg().get("enable", True):
            return
        try:
            try:
                songs = await kgapi.personal_fm()
            except ApiError:
                songs = await kgapi.everyday_recommend()
            if not songs:
                await self._reply(event, "没有拿到推荐歌曲，请稍后再试")
                event.stop_event()
                return
            song = random.choice(songs)
            await self._play_song(event, song, source="推荐")
        except ApiError as err:
            self._log_warn(f"来首歌失败: {err}")
            await self._reply(event, f"随机点歌失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*FM$", re.IGNORECASE))
    async def fm(self, event: AstrMessageEvent):
        """#kgFM：私人 FM 歌曲列表"""
        if not self._cfg().get("enable", True):
            return
        try:
            songs = await kgapi.personal_fm()
            if not songs:
                await self._reply(event, "暂无 FM 歌曲")
                event.stop_event()
                return
            await self._list_to_session(event, "私人 FM", songs[:15])
        except ApiError as err:
            self._log_warn(f"FM 失败: {err}")
            await self._reply(event, f"获取 FM 失败：{err}\n可能需要 #kg登录")
        event.stop_event()

    # ══════════════════ 账号 · 扩展（需登录） ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*我的歌单$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def my_playlist(self, event: AstrMessageEvent):
        """#kg我的歌单：我创建/收藏的歌单（需登录）"""
        if not self._cfg().get("enable", True):
            return
        uid = await self._get_uid()
        if not uid:
            await self._reply(event, "需要登录后使用，请先 #kg登录")
            event.stop_event()
            return
        try:
            pls = await kgapi.user_playlist(uid)
            if not pls:
                await self._reply(event, "暂无歌单")
                event.stop_event()
                return
            data = cardlib.build_playlist_card_data("我的歌单", pls, subtitle="我创建/收藏的歌单", cfg=self._cfg())
            await self._reply_card_or_text(
                event,
                tpl_name="kg-playlist",
                data=data,
                format_text=lambda d: cardlib.format_playlist_text("我的歌单", pls),
            )
        except ApiError as err:
            self._log_warn(f"我的歌单失败: {err}")
            await self._reply(event, f"获取歌单失败：{err}\n需要先 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*最近$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def recent_song(self, event: AstrMessageEvent):
        """#kg最近：最近播放歌曲（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        try:
            songs = await kgapi.user_listen(pagesize=30)
            if not songs:
                await self._reply(event, "暂无最近播放记录")
                event.stop_event()
                return
            await self._list_to_session(event, "最近播放", songs[:20])
        except ApiError as err:
            self._log_warn(f"最近播放失败: {err}")
            await self._reply(event, f"获取最近播放失败：{err}\n需要先 #kg登录")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*听歌排行$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def user_history(self, event: AstrMessageEvent):
        """#kg听歌排行：听歌排行（需登录）"""
        if not self._cfg().get("enable", True):
            return
        if not await self._require_login(event):
            return
        try:
            songs = await kgapi.user_history(pagesize=30)
            if not songs:
                await self._reply(event, "暂无听歌排行记录")
                event.stop_event()
                return
            await self._list_to_session(event, "听歌排行", songs[:20])
        except ApiError as err:
            self._log_warn(f"听歌排行失败: {err}")
            await self._reply(event, f"获取听歌排行失败：{err}\n需要先 #kg登录")
        event.stop_event()

    # ══════════════════ 账号 ══════════════════

    async def _get_uid(self) -> str:
        uid = str(self._cfg().get("defaultUid") or "")
        if uid:
            return uid
        cookie = str(self._cfg().get("defaultCookie") or "")
        m = re.search(r"(?:^|[;])\s*userid=([\w.-]+)", cookie)
        if m:
            uid = m.group(1)
            self.config["defaultUid"] = uid
            self.config.save_config()
        return uid

    @filter.regex(re.compile(r"^#?(?:kg登录|kg扫码登录|酷狗登录|酷狗扫码登录)$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_qr_login(self, event: AstrMessageEvent):
        """#kg登录：扫码登录酷狗账号"""
        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        if cfg.get("qrLoginEnable") is False:
            await self._reply(event, "扫码登录已在配置中关闭")
            event.stop_event()
            return
        user_key = self._user_key(event)
        self._stop_poll(user_key)
        try:
            await self._reply(event, "正在获取酷狗登录二维码…")
            key = await kgapi.qr_key()
            if not key:
                await self._reply(event, "获取二维码失败：无法获取 qrcode，请检查 API 服务")
                event.stop_event()
                return
            info = await kgapi.qr_create(key)
            qrurl = info.get("url") or ""
            qrimg = info.get("base64") or ""
            tip_text = "请使用酷狗音乐 App 扫码登录\n二维码约 5 分钟内有效"
            qr_path = await self._save_qr_image(qrimg) if qrimg else None
            img_sent = False
            if qr_path:
                try:
                    await self._send_chain(event, Image.fromFileSystem(qr_path), self._plain(tip_text))
                    img_sent = True
                except Exception:
                    pass
                asyncio.get_running_loop().call_later(120, lambda: self._safe_unlink(qr_path))
            if not img_sent:
                await self._reply(event, tip_text + (f"\n或打开链接扫码：{qrurl}" if qrurl else ""))
            self._start_poll(event, key, 300)
        except ApiError as err:
            await self._reply(event, f"扫码登录失败：{err}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kgqq登录|kgqq扫码登录|酷狗qq登录|酷狗qq扫码登录)$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_qq_qr_login(self, event: AstrMessageEvent):
        """#kgqq登录：通过 QQ 扫码登录绑定酷狗账号"""
        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        if cfg.get("qrLoginEnable") is False:
            await self._reply(event, "扫码登录已在配置中关闭")
            event.stop_event()
            return
        user_key = self._user_key(event)
        self._stop_poll(user_key)
        try:
            await self._reply(event, "正在获取 QQ 登录二维码…")
            info = await kgapi.login_qq_qr_create()
            qrimg = info.get("qrcode") or ""
            if not qrimg:
                await self._reply(event, "获取 QQ 二维码失败，请检查 API 服务")
                event.stop_event()
                return
            tip_text = "请使用手机 QQ 扫码授权登录酷狗\n二维码约 2 分钟内有效"
            qr_path = await self._save_qr_image(qrimg)
            img_sent = False
            if qr_path:
                try:
                    await self._send_chain(event, Image.fromFileSystem(qr_path), self._plain(tip_text))
                    img_sent = True
                except Exception:
                    pass
                asyncio.get_running_loop().call_later(120, lambda: self._safe_unlink(qr_path))
            if not img_sent:
                await self._reply(event, tip_text)
            self._start_qq_poll(event, info, 180)
        except ApiError as err:
            await self._reply(event, f"QQ 扫码登录失败：{err}")
        event.stop_event()

    def _start_qq_poll(self, event: AstrMessageEvent, qr_ctx: dict, max_sec: int = 180):
        user_key = self._user_key(event)
        started = time.time()
        qrsig = qr_ctx.get("qrsig") or ""
        task = {
            "key": f"qq_{qrsig}",
            "ctx": qr_ctx,
            "stopped": False,
            "busy": False,
            "notifiedScan": False,
            "failStreak": 0,
        }
        self._active_logins[user_key] = task
        loop = asyncio.get_running_loop()

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            if time.time() - started > max_sec:
                task["stopped"] = True
                self._active_logins.pop(user_key, None)
                await self._reply(event, "QQ 二维码已过期，请重新 #kgqq登录")
                return
            task["busy"] = True
            try:
                check_params = {
                    "qrsig": task["ctx"].get("qrsig") or "",
                    "ptqrtoken": task["ctx"].get("ptqrtoken") or "",
                    "pt_login_sig": task["ctx"].get("pt_login_sig") or "",
                    "pt_openlogin_data": task["ctx"].get("pt_openlogin_data") or "",
                    "xlogin_url": task["ctx"].get("xlogin_url") or "",
                    "cookie": task["ctx"].get("cookie") or "",
                }
                info = await kgapi.login_qq_qr_check(check_params)
                status = str(info.get("status") if info.get("status") is not None else "")
                if status in ("expired", "65"):
                    task["stopped"] = True
                    self._active_logins.pop(user_key, None)
                    await self._reply(event, "QQ 二维码已失效，请重新 #kgqq登录")
                    return
                if (status in ("wait", "66") or "扫码" in str(info.get("msg") or "")) and not task["notifiedScan"]:
                    if "确认" in str(info.get("msg") or ""):
                        task["notifiedScan"] = True
                        await self._reply(event, "已扫码，请在手机 QQ 上确认授权登录")
                elif status in ("0", "1") or info.get("token"):
                    # 授权成功换取了 token
                    await self._finish_login(event, info, user_key, task)
                    return
                task["failStreak"] = 0
            except Exception as err:
                task["failStreak"] += 1
                if task["failStreak"] == 5:
                    await self._reply(event, f"QQ 轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self._active_logins.pop(user_key, None)
                    await self._reply(event, "QQ 轮询失败过多，请检查 API 服务或重新 #kgqq登录")
                    return
            finally:
                task["busy"] = False
            if not task["stopped"] and self._active_logins.get(user_key, {}).get("key") == task["key"]:
                task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    def _stop_poll(self, user_key: str):
        task = self._active_logins.pop(user_key, None)
        if task and task.get("timer") is not None:
            try:
                task["timer"].cancel()
            except Exception:
                pass

    def _start_poll(self, event: AstrMessageEvent, key: str, max_sec: int = 300):
        user_key = self._user_key(event)
        started = time.time()
        task = {"key": key, "stopped": False, "busy": False, "notifiedScan": False, "failStreak": 0}
        self._active_logins[user_key] = task
        loop = asyncio.get_running_loop()

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            if time.time() - started > max_sec:
                task["stopped"] = True
                self._active_logins.pop(user_key, None)
                await self._reply(event, "二维码已过期，请重新 #kg登录")
                return
            task["busy"] = True
            try:
                info = await kgapi.qr_check(key)
                status = info.get("status")
                if status == 0:
                    task["stopped"] = True
                    self._active_logins.pop(user_key, None)
                    await self._reply(event, "二维码已失效，请重新 #kg登录")
                    return
                if status == 2 and not task["notifiedScan"]:
                    task["notifiedScan"] = True
                    await self._reply(event, "已扫码，请在手机上确认登录")
                elif status == 4:
                    await self._finish_login(event, info, user_key, task)
                    return
                task["failStreak"] = 0
            except Exception as err:
                task["failStreak"] += 1
                if task["failStreak"] == 5:
                    await self._reply(event, f"轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self._active_logins.pop(user_key, None)
                    await self._reply(event, "轮询失败过多，请检查 API 服务或重新 #kg登录")
                    return
            finally:
                task["busy"] = False
            if not task["stopped"] and self._active_logins.get(user_key, {}).get("key") == key:
                task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    async def _finish_login(self, event: AstrMessageEvent, info: dict, user_key: str, task: dict):
        task["stopped"] = True
        self._active_logins.pop(user_key, None)
        token = info.get("token") or ""
        userid = info.get("userid") or ""
        nickname = info.get("nickname") or ""
        if not token or not userid:
            await self._reply(event, "登录成功但未获取到 Cookie（可能登录状态异常），请重新 #kg登录")
            return
        cookie = f"token={token};userid={userid}"
        try:
            self.config["defaultCookie"] = cookie
            self.config["defaultUid"] = str(userid)
            self.config.save_config()
            self._log_info("扫码登录成功，Cookie 已写入插件配置 defaultCookie")
        except Exception as e:
            self._log_warn(f"写入默认 Cookie 失败: {e}")
        await self._reply(
            event, f"✅ 登录成功：{nickname or userid or '已写入 Cookie'}\nCookie 已存入插件配置，全群默认使用该账号"
        )
        # 刷新登录 token 延长过期时间（fire-and-forget）
        try:
            asyncio.create_task(self._refresh_token_once())
        except Exception:
            pass
        await self._send_status(event, status_data=None)

    async def _refresh_token_once(self):
        try:
            await kgapi.login_token_refresh()
            self._log_info("已刷新酷狗登录 token")
        except ApiError as e:
            self._log_warn(f"刷新登录 token 失败: {e}")

    async def _build_status(self, *, status_data: dict | None = None) -> dict:
        cfg = self._cfg()
        default_cookie = str(cfg.get("defaultCookie") or "")
        status = {
            "loggedIn": False,
            "nickname": "",
            "avatar": "",
            "uin": "",
            "level": "",
            "vipLabel": "",
            "apiBase": cfg.get("apiBase") or "",
            "keyStatus": "默认 Cookie" if default_cookie else "无 Cookie",
            "quality": str(cfg.get("quality") or "auto"),
        }
        if status_data is not None:
            # 登录刚成功：直接从登录信息构建
            status["loggedIn"] = True
            status["nickname"] = status_data.get("nickname") or ""
            status["uin"] = str(status_data.get("userid") or "")
            return status
        if not default_cookie:
            return status
        uid = await self._get_uid()
        if not uid:
            status["keyStatus"] = "Cookie 未含 userid，无法校验"
            return status
        try:
            info = await kgapi.user_detail(uid)
            if info and info.get("name"):
                status["loggedIn"] = True
                status["nickname"] = info.get("name") or ""
                status["avatar"] = info.get("avatar") or ""
                status["uin"] = str(info.get("id") or uid)
                if info.get("level"):
                    status["level"] = str(info["level"])
                if info.get("vip"):
                    status["vipLabel"] = "酷狗 VIP"
            else:
                status["keyStatus"] = "Cookie 可能已失效"
        except ApiError as e:
            status["keyStatus"] = f"查询失败：{e}"
        return status

    async def _send_status(self, event: AstrMessageEvent, *, status_data: dict | None = None):
        try:
            status = await self._build_status(status_data=status_data)
            data = cardlib.build_status_card_data(status)
            await self._reply_card_or_text(
                event, tpl_name="kg-status", data=data, format_text=lambda d: cardlib.format_status_text(status)
            )
        except Exception as err:
            self._log_warn(f"状态卡片失败: {err}")
            await self._reply(
                event,
                cardlib.format_status_text(
                    {
                        "loggedIn": False,
                        "apiBase": self._cfg().get("apiBase") or "",
                        "quality": str(self._cfg().get("quality") or "auto"),
                        "keyStatus": str(err),
                    }
                ),
            )

    @filter.regex(re.compile(r"^#?(?:kg状态|kg登录状态|kgs)$", re.IGNORECASE), priority=6)
    async def login_status_cmd(self, event: AstrMessageEvent):
        """#kg状态 / #kgs：查看登录状态"""
        if not self._cfg().get("enable", True):
            return
        await self._send_status(event)
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg登出|kg注销|kg解绑)$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def logout(self, event: AstrMessageEvent):
        """#kg登出：清除酷狗登录 Cookie"""
        try:
            if self.config.get("defaultCookie"):
                self.config["defaultCookie"] = ""
                self.config["defaultUid"] = ""
                self.config.save_config()
                await self._reply(event, "已登出酷狗账号，并清除插件配置中的默认 Cookie")
            else:
                await self._reply(event, "当前未配置登录 Cookie")
        except Exception as err:
            await self._reply(event, f"登出失败：{err}")
        event.stop_event()

    # ══════════════════ 管理 ══════════════════

    @filter.regex(re.compile(r"^#?(?:kg设置|kg配置|酷狗设置)$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def settings(self, event: AstrMessageEvent):
        """#kg设置：查看插件设置"""
        cfg = self._cfg()
        try:
            uid = await self._get_uid()
        except Exception:
            uid = ""
        data = cardlib.build_settings_card_data(cfg, uid)
        await self._reply_card_or_text(
            event,
            tpl_name="kg-settings",
            data=data,
            format_text=lambda d: cardlib.format_settings_text(cfg, uid),
        )
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*音质\s*(.+)$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def quality_cmd(self, event: AstrMessageEvent):
        """#kg音质 <档位>：修改音质（auto/flac/320/128）"""
        m = re.match(r"^#?(?:kg|KG)\s*音质\s*(.+)$", event.message_str.strip(), re.IGNORECASE)
        q = (m.group(1).strip().lower() if m else "").strip()
        if q not in QUALITY_LABEL:
            await self._reply(event, f"音质档位无效。可选：{' / '.join(QUALITY_LABEL.keys())}")
            event.stop_event()
            return
        self.config["quality"] = q
        self.config.save_config()
        await self._reply(event, f"已设置音质：{QUALITY_LABEL.get(q, q)}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg|KG)\s*api\s*(https?://\S+)$", re.IGNORECASE))
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def api_cmd(self, event: AstrMessageEvent):
        """#kg api <地址>：修改 API 地址"""
        m = re.match(r"^#?(?:kg|KG)\s*api\s*(https?://\S+)$", event.message_str.strip(), re.IGNORECASE)
        url = m.group(1).strip().rstrip("/") if m else ""
        self.config["apiBase"] = url
        self.config.save_config()
        await self._reply(event, f"已设置 API 地址：{url}")
        event.stop_event()

    @filter.regex(re.compile(r"^#?(?:kg测试|酷狗测试)$", re.IGNORECASE), priority=6)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def api_test(self, event: AstrMessageEvent):
        """#kg测试：测试 API 连通"""
        cfg = self._cfg()
        base = str(cfg.get("apiBase") or "")
        if not base:
            await self._reply(event, "⚠ API 未配置，请使用 #kg api <地址> 配置")
            event.stop_event()
            return
        try:
            lst = await kgapi.search("测试", pagesize=1)
            masked = cardlib.mask_api_base(base)
            await self._reply(event, f"✅ API 连通正常：{masked}\n搜索结果 {len(lst)} 条")
        except ApiError as e:
            await self._reply(event, f"❌ API 连接失败：{e}")
        event.stop_event()

    # ══════════════════ 链接自动解析 ══════════════════

    @filter.regex(re.compile(r"kugou\.com|kugou\.net", re.IGNORECASE))
    async def resolve(self, event: AstrMessageEvent):
        """酷狗音乐链接自动解析"""
        cfg = self._cfg()
        if not cfg.get("enable", True) or cfg.get("enableResolve") is False:
            return
        text = _collect_message_text(event)
        if not _is_kg_message(text):
            return
        if _is_plugin_command_msg(event.message_str):
            return
        handled = await self._handle_resolve(event, text)
        if handled:
            event.stop_event()

    async def _handle_resolve(self, event: AstrMessageEvent, text: str) -> bool:
        try:
            h = kgapi.extract_kugou_hash(text)
            if h and re.fullmatch(r"[0-9A-Fa-f]{32}", h):
                song = await kgapi.audio_by_hash(h.upper())
                if not song:
                    await self._reply(event, "该歌曲不存在或无版权")
                    return True
                await self._play_song(event, song, source="链接解析")
                return True
            if h:
                # 提取到的是 mixsongid（数字）：KuGouMusicApi 的 /audio 只支持
                # 32 位文件 hash，无法据此取链，明确提示而非静默失败
                await self._reply(event, "该链接为 mixsongid 形式，暂不支持自动解析，可用 #kg点歌 关键词 代替")
                return True
            # 无 hash：把链接去掉后当关键词搜索
            kw = re.sub(r"https?://\S+|\[CQ:[^\]]*\]", "", text).strip()
            kw = re.sub(r"kugou\.com|酷狗|分享|歌曲|链接", "", kw, flags=re.IGNORECASE).strip()
            if len(kw) >= 2:
                lst = await kgapi.search(kw, "song", pagesize=1)
                if lst:
                    await self._play_song(event, lst[0], source="链接解析")
                    return True
            await self._reply(event, "无法解析该酷狗链接（暂支持 hash 歌曲链接）")
            return True
        except ApiError as err:
            self._log_warn(f"解析失败: {err}")
            await self._reply(event, f"解析失败：{err}")
            return True

    # ══════════════════ 生命周期 ══════════════════

    async def terminate(self):
        for user_key in list(self._active_logins.keys()):
            self._stop_poll(user_key)
