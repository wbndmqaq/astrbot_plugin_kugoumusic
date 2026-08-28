from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain

if TYPE_CHECKING:
    from ..main import KugouMusicPlugin

try:
    from . import api as kgapi
    from . import cards as cardlib
    from .api import ApiError
    from .delivery import _write_bytes, deliver_song, get_temp_dir
    from .quality import QUALITY_LABEL, trial_label
    from .render import render_card_png
except ImportError:
    from core import api as kgapi
    from core import cards as cardlib
    from core.api import ApiError
    from core.delivery import _write_bytes, deliver_song, get_temp_dir
    from core.quality import QUALITY_LABEL, trial_label
    from core.render import render_card_png

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)

# #kg听所有 连播上限（防止误触发刷屏/大量下载）
PLAY_ALL_LIMIT = 30

NEW_ALBUM_AREAS = {"": 0, "推荐": 0, "华语": 1, "欧美": 2, "日本": 3, "韩国": 4}
GOOD_SONG_CARDS = {"精选": 1, "怀旧": 2, "热门": 3, "小众": 4, "vip": 6, "VIP": 6}
ARTIST_LIST_TYPES = {"": 0, "全部": 0, "华语": 1, "欧美": 2, "日韩": 3, "其他": 4, "日本": 5, "韩国": 6}


def is_plugin_command_msg(msg: str) -> bool:
    return bool(
        re.match(
            r"^#?(?:kg|KG)(?:[^\x00-\x7F]|\b)|^#?\s*听\s*[1-9]|^#?(kg|KG)听\s*[1-9]",
            str(msg or "").strip(),
            re.IGNORECASE,
        )
    )


def is_kg_message(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"kugou\.com|kugou\.net", text, re.IGNORECASE))


def collect_message_text(event: AstrMessageEvent) -> str:
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
        for seg in chain:
            seg_type = type(seg).__name__
            if isinstance(seg, Plain):
                t = seg.text if hasattr(seg, "text") else None
                if t:
                    parts.append(str(t))
            elif seg_type == "Json":
                # OneBot json 段（音乐分享卡片）：适配器不写入 message_str，
                # 必须从组件 data 里取 ark JSON，否则分享卡永远解析不到
                d = getattr(seg, "data", None)
                if d:
                    parts.append(str(d))
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


class MusicService:
    """酷狗音乐业务服务层，封装状态管理、卡片渲染、歌曲解析播放、扫码登录等。"""

    def __init__(self, plugin: KugouMusicPlugin):
        self.plugin = plugin
        self.active_logins: dict[str, dict[str, Any]] = {}
        # 注入配置访问器给 api 模块
        kgapi.set_config_getter(lambda: self.plugin.config or {})

    # ──────────── 生命周期 ────────────

    async def initialize(self):
        """启动时确保设备 dfid（酷狗取链/搜索需要），持久化在 plugin_data 下避免重复注册。"""
        try:
            await self.ensure_device()
        except Exception as e:
            self.log_warn(f"初始化设备 Cookie 失败: {e}")

    async def ensure_device(self):
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
                self.log_info(f"已注册酷狗设备 dfid（{dfid[:6]}…）")
            except Exception as e:
                self.log_warn(f"持久化设备 Cookie 失败: {e}")

    def terminate(self):
        for user_key in list(self.active_logins.keys()):
            self.stop_poll(user_key)

    # ──────────── 辅助 ────────────

    @property
    def config(self) -> dict:
        return self.plugin.config or {}

    def cfg(self) -> dict:
        return self.plugin.config or {}

    def log_warn(self, msg: str):
        logger.warning(f"[kugou] {msg}")

    def log_info(self, msg: str):
        logger.info(f"[kugou] {msg}")

    def plain(self, text: str) -> Plain:
        return Plain(text=text)

    async def send_chain(self, event: AstrMessageEvent, *components):
        comps = [c for c in components if c is not None]
        if not comps:
            return
        mc = MessageChain(chain=list(comps))
        mc.use_markdown_ = False
        try:
            await event.send(mc)
        except AttributeError:
            import traceback as _tb

            self.log_warn(f"_send_chain 发送失败（AttributeError）:\n{_tb.format_exc()}")
            texts = []
            for _c in comps:
                t = getattr(_c, "text", None)
                if t:
                    texts.append(str(t))
            if texts:
                try:
                    _fb = MessageChain(chain=[self.plain("\n".join(texts))])
                    _fb.use_markdown_ = False
                    await event.send(_fb)
                except Exception as _e2:
                    self.log_warn(f"_send_chain 文本兜底也失败: {_e2}")

    async def reply(self, event: AstrMessageEvent, text: str):
        try:
            await self.send_chain(event, self.plain(text))
        except Exception as e:
            import traceback as _tb

            self.log_warn(f"_reply 发送失败: {e}\n{_tb.format_exc()}")

    def scope(self, event: AstrMessageEvent) -> str:
        gid = getattr(event.message_obj, "group_id", None)
        if gid:
            return str(gid)
        return event.get_sender_id()

    def user_key(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "")

    def check_cmd(self, event: AstrMessageEvent, pattern: str, *, song_request: bool = False) -> re.Match | None:
        cfg = self.cfg()
        if not cfg.get("enable", True):
            return None
        if song_request and cfg.get("enableSongRequest") is False:
            return None
        return re.match(pattern, event.message_str.strip(), re.IGNORECASE)

    # ──────────── 关键词 → 资源解析 ────────────

    async def resolve_song(self, kw: str) -> dict | None:
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

    async def resolve_playlist(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        if re.fullmatch(r"\d+", kw):
            return {"id": kw, "name": "", "cover": "", "songCount": 0, "creator": ""}
        pls = await kgapi.search(kw, "special", pagesize=5)
        return pls[0] if pls else None

    async def resolve_album(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        if re.fullmatch(r"\d+", kw):
            return {"id": kw, "name": "", "cover": "", "artist": ""}
        albums = await kgapi.search(kw, "album", pagesize=5)
        return albums[0] if albums else None

    async def resolve_artist(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        if re.fullmatch(r"\d+", kw):
            return {"id": kw, "name": "", "cover": ""}
        artists = await kgapi.search(kw, "author", pagesize=5)
        return artists[0] if artists else None

    # ──────────── 先选歌再操作 ────────────

    async def start_select(self, event: AstrMessageEvent, action: str, kw: str, *, label: str, verb: str) -> None:
        """进入"先选歌再操作"流程。

        带关键词→搜索出候选列表；不带→复用会话列表（无列表则提示先搜）。
        列表写入会话并记录 ``action``（#kg听N 消费后一次性执行，用完恢复播放）。
        """
        scope = self.scope(event)
        session = await cardlib.SessionStore.get(self.plugin, scope)
        if (kw or "").strip():
            page_size = min(int(self.cfg().get("maxList") or 10), 20)
            lst = await kgapi.search(kw, "song", pagesize=page_size)
            if not lst:
                await self.reply(event, f"没有搜到「{kw}」")
                return
            keyword = kw
        else:
            lst = (session or {}).get("data") or []
            if not lst:
                await self.reply(
                    event,
                    f"用法：先 #kg点歌 关键词 选中歌曲，再发 #kg{label}；或直接 #kg{label} 关键词 选择",
                )
                return
            keyword = (session or {}).get("keyword") or "当前会话"
        base = dict(session) if session else {}
        base.update({"type": "kg_songs", "keyword": keyword, "data": lst, "action": action})
        await cardlib.SessionStore.set(self.plugin, scope, base)
        tip = f"回复 #kg听N 即可{verb}"
        text = cardlib.format_song_list(lst, keyword, tip=tip)
        if self.cfg().get("renderListCard", True):
            data = cardlib.build_list_card_data(keyword, lst, options={"tip": tip}, cfg=self.cfg())
            if await self.reply_card_or_text(event, tpl_name="kg-list", data=data, format_text=lambda d: text):
                return
        await self.reply(event, text)

    async def show_lyric(self, event: AstrMessageEvent, song: dict) -> None:
        lines = await self.fetch_lyric(song)
        if not lines:
            await self.reply(event, "该歌曲暂无歌词")
            return
        await self.send_lyric_pages(event, song, lines, base_tip="歌词来自酷狗音乐")

    async def show_lyric_word(self, event: AstrMessageEvent, song: dict) -> None:
        lines = await self.fetch_krc_lyric(song)
        if not lines:
            # 无逐字歌词时退回普通歌词，避免只给一句死提示
            await self.reply(event, "该歌曲暂无逐字歌词，已改为显示普通歌词")
            await self.show_lyric(event, song)
            return
        await self.send_lyric_pages(event, song, lines, base_tip="逐字歌词来自酷狗音乐")

    async def send_lyric_pages(self, event: AstrMessageEvent, song: dict, lines: list, *, base_tip: str) -> None:
        """歌词分页发送：每页最多 36 行，分多张卡片完整展示（不再截断）。"""
        pages = [lines[i : i + 36] for i in range(0, len(lines), 36)]
        total = len(lines)
        for pi, page_lines in enumerate(pages):
            data = cardlib.build_lyric_card_data(song, page_lines, line_count=total)
            data["tip"] = (
                f"{base_tip} · 第 {pi + 1}/{len(pages)} 页" if len(pages) > 1 else base_tip
            )
            ok = await self.reply_card_or_text(
                event,
                tpl_name="kg-lyric",
                data=data,
                format_text=lambda d: cardlib.format_lyric_text(song, d.get("lines") or []),
            )
            if not ok:
                break

    async def show_comment(self, event: AstrMessageEvent, song: dict) -> None:
        mix = song.get("mixsongid") or song.get("id") or ""
        if not mix:
            await self.reply(event, "无法获取该歌曲评论（缺少歌曲 ID）")
            return
        r = await kgapi.comment_music(mix, pagesize=20)
        if not r.get("comments"):
            await self.reply(event, "该歌曲暂无评论")
            return
        data = cardlib.build_comment_card_data(song, r["comments"], total=r.get("count"))
        await self.reply_card_or_text(
            event,
            tpl_name="kg-comment",
            data=data,
            format_text=lambda d: cardlib.format_comment_text(song, r["comments"]),
        )

    async def show_mv(self, event: AstrMessageEvent, song: dict) -> None:
        mvs = await kgapi.search(song.get("name") or "", "mv", pagesize=5)
        if not mvs:
            await self.reply(event, "该歌曲暂无 MV")
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
        await self.reply(event, "\n".join(lines))

    async def show_favorite(self, event: AstrMessageEvent, song: dict) -> None:
        mix = song.get("mixsongid") or song.get("id") or ""
        cnt = await kgapi.favorite_count(mix)
        await self.reply(event, f"⭐ 收藏数：{cnt or '未知'}\n♪ {song['name']} - {song['artist']}")

    async def show_versions(self, event: AstrMessageEvent, song: dict) -> None:
        mix = song.get("mixsongid") or song.get("id") or ""
        songs = await kgapi.related_songs(mix)
        if not songs:
            await self.reply(event, "暂无其他版本")
            return
        await self.list_to_session(event, f"更多版本 · {song['name']}", songs)

    # ──────────── 登录态 ────────────

    def has_cookie(self) -> bool:
        return bool(self.cfg().get("defaultCookie"))

    async def require_login(self, event: AstrMessageEvent) -> bool:
        """未登录时提示并消费事件，返回 False（调用方需立即 return）。"""
        if not self.has_cookie():
            await self.reply(event, "需要登录后使用，请先 #kg登录")
            event.stop_event()
            return False
        return True

    # ──────────── 取链 ────────────

    async def resolve_play(self, song: dict, cfg: dict) -> dict:
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

    async def play_song(self, event: AstrMessageEvent, song: dict, *, source: str = "") -> None:
        cfg = self.cfg()
        play = await self.resolve_play(song, cfg)
        quality_label = trial_label(play)
        if play.get("url"):
            tip = "正在下载并发送语音/文件…"
        elif play.get("error"):
            tip = play["error"]
        else:
            tip = "⚠ 未获取到播放链接，可尝试 #kg登录 或换个音质"
        data = cardlib.build_detail_card_data(song, quality_label, source=source, tip=tip)
        await self.reply_card_or_text(
            event,
            tpl_name="kg-detail",
            data=data,
            format_text=lambda d: cardlib.format_detail_text(song, play, tip),
        )
        if play.get("url"):
            await deliver_song(self.plugin, event, song, play, cfg=cfg, plugin_dir=PLUGIN_DIR)
            self.report_play_history(song)

    def report_play_history(self, song: dict):
        """播放后自动上报听歌历史（需登录；fire-and-forget，失败不打扰）。"""
        if not self.has_cookie():
            return
        mxid = song.get("mixsongid") or song.get("id") or ""
        if not mxid:
            return

        async def _do():
            try:
                await kgapi.playhistory_upload(mxid)
            except ApiError as e:
                self.log_warn(f"上报听歌历史失败: {e}")

        try:
            asyncio.create_task(_do())
        except Exception:
            pass

    async def list_to_session(self, event: AstrMessageEvent, keyword: str, songs: list, *, tip: str = "") -> bool:
        scope = self.scope(event)
        await cardlib.SessionStore.set(self.plugin, scope, {"type": "kg_songs", "keyword": keyword, "data": songs})
        text = cardlib.format_song_list(songs, keyword, tip=tip)
        if self.cfg().get("renderListCard", True):
            data = cardlib.build_list_card_data(keyword, songs, options={"tip": tip}, cfg=self.cfg())
            if await self.reply_card_or_text(event, tpl_name="kg-list", data=data, format_text=lambda d: text):
                return True
        await self.reply(event, text)
        return True

    # ──────────── 卡片渲染 ────────────

    async def render_card(self, event: AstrMessageEvent, data: dict, tpl_name: str) -> str | None:
        try:
            tmpl_path = os.path.join(PLUGIN_DIR, "resources", "html", tpl_name, f"{tpl_name}.html")
            if not os.path.exists(tmpl_path):
                return None
            raw = await render_card_png(tmpl_path, data)
            if raw is None:
                return None
            d = get_temp_dir(self.cfg(), PLUGIN_DIR)
            file_path = os.path.join(d, f"card_{tpl_name}_{int(time.time() * 1000)}.png")
            await asyncio.to_thread(_write_bytes, file_path, raw)
            return file_path
        except Exception as e:
            self.log_warn(f"{tpl_name} 本地渲染失败: {e}")
            return None

    async def reply_card_or_text(self, event: AstrMessageEvent, *, tpl_name: str, data: dict, format_text) -> bool:
        card_path = None
        try:
            card_path = await self.render_card(event, data, tpl_name)
            if card_path:
                await self.send_chain(event, Image.fromFileSystem(card_path))
                return True
        except Exception as e:
            self.log_warn(f"{tpl_name} 卡片渲染失败，回退文本: {e}")
        finally:
            if card_path:
                asyncio.get_running_loop().call_later(
                    max(0, int(self.cfg().get("keepFileSec", 60))),
                    lambda: self.safe_unlink(card_path),
                )
        try:
            text = format_text(data)
            if text:
                await self.send_chain(event, self.plain(text))
                return True
        except Exception as e:
            self.log_warn(f"{tpl_name} 文本兜底失败: {e}")
        return False

    async def save_qr_image(self, b64: str) -> str | None:
        try:
            import base64

            raw = b64
            if "," in raw and raw.split(",", 1)[0].startswith("data:"):
                raw = raw.split(",", 1)[1]
            data = base64.b64decode(raw)
            path = os.path.join(get_temp_dir(self.cfg(), PLUGIN_DIR), f"qr_{int(time.time() * 1000)}.png")
            await asyncio.to_thread(_write_bytes, path, data)
            return path
        except Exception as e:
            self.log_warn(f"保存二维码失败: {e}")
            return None

    def safe_unlink(self, path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    async def fetch_lyric(self, song: dict) -> list:
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
        return self.extract_lyric_lines(content)

    @staticmethod
    def extract_lyric_lines(lrc: str) -> list:
        """LRC 文本 → 纯文本行，完整返回（分页由调用方处理）。"""
        def _strip_meta(lines):
            return [l for l in lines if not re.match(r"^\s*\[(ti|ar|al|by|offset|total):", l, re.IGNORECASE)]

        out = []
        for l in _strip_meta(lrc.splitlines()):
            t = re.sub(r"^\[[^\]]*\]", "", l).strip()
            if t:
                out.append(t)
        return out

    async def fetch_krc_lyric(self, song: dict) -> list:
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
        return out

    # ──────────── 登录辅助与轮询 ────────────

    async def get_uid(self) -> str:
        uid = str(self.cfg().get("defaultUid") or "")
        if uid:
            return uid
        cookie = str(self.cfg().get("defaultCookie") or "")
        m = re.search(r"(?:^|[;])\s*userid=([\w.-]+)", cookie)
        if m:
            uid = m.group(1)
            self.plugin.config["defaultUid"] = uid
            self.plugin.config.save_config()
        return uid

    def stop_poll(self, user_key: str):
        task = self.active_logins.pop(user_key, None)
        if task and task.get("timer") is not None:
            try:
                task["timer"].cancel()
            except Exception:
                pass

    def start_poll(self, event: AstrMessageEvent, key: str, max_sec: int = 300):
        user_key = self.user_key(event)
        started = time.time()
        task = {"key": key, "stopped": False, "busy": False, "notifiedScan": False, "failStreak": 0}
        self.active_logins[user_key] = task
        loop = asyncio.get_running_loop()

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            if time.time() - started > max_sec:
                task["stopped"] = True
                self.active_logins.pop(user_key, None)
                await self.reply(event, "二维码已过期，请重新 #kg登录")
                return
            task["busy"] = True
            try:
                info = await kgapi.qr_check(key)
                status = info.get("status")
                if status == 0:
                    task["stopped"] = True
                    self.active_logins.pop(user_key, None)
                    await self.reply(event, "二维码已失效，请重新 #kg登录")
                    return
                if status == 2 and not task["notifiedScan"]:
                    task["notifiedScan"] = True
                    await self.reply(event, "已扫码，请在手机上确认登录")
                elif status == 4:
                    await self.finish_login(event, info, user_key, task)
                    return
                task["failStreak"] = 0
            except Exception as err:
                task["failStreak"] += 1
                if task["failStreak"] == 5:
                    await self.reply(event, f"轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self.active_logins.pop(user_key, None)
                    await self.reply(event, "轮询失败过多，请检查 API 服务或重新 #kg登录")
                    return
            finally:
                task["busy"] = False
            if not task["stopped"] and self.active_logins.get(user_key, {}).get("key") == key:
                task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    def start_qq_poll(self, event: AstrMessageEvent, qr_ctx: dict, max_sec: int = 180):
        user_key = self.user_key(event)
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
        self.active_logins[user_key] = task
        loop = asyncio.get_running_loop()

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            if time.time() - started > max_sec:
                task["stopped"] = True
                self.active_logins.pop(user_key, None)
                await self.reply(event, "QQ 二维码已过期，请重新 #kgqq登录")
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
                    self.active_logins.pop(user_key, None)
                    await self.reply(event, "QQ 二维码已失效，请重新 #kgqq登录")
                    return
                if (status in ("wait", "66") or "扫码" in str(info.get("msg") or "")) and not task["notifiedScan"]:
                    if "确认" in str(info.get("msg") or ""):
                        task["notifiedScan"] = True
                        await self.reply(event, "已扫码，请在手机 QQ 上确认授权登录")
                elif status in ("0", "1") or info.get("token"):
                    # 授权成功换取了 token
                    await self.finish_login(event, info, user_key, task)
                    return
                task["failStreak"] = 0
            except Exception as err:
                task["failStreak"] += 1
                if task["failStreak"] == 5:
                    await self.reply(event, f"QQ 轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self.active_logins.pop(user_key, None)
                    await self.reply(event, "QQ 轮询失败过多，请检查 API 服务或重新 #kgqq登录")
                    return
            finally:
                task["busy"] = False
            if not task["stopped"] and self.active_logins.get(user_key, {}).get("key") == task["key"]:
                task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    async def finish_login(self, event: AstrMessageEvent, info: dict, user_key: str, task: dict):
        task["stopped"] = True
        self.active_logins.pop(user_key, None)
        token = info.get("token") or ""
        userid = info.get("userid") or ""
        nickname = info.get("nickname") or ""
        if not token or not userid:
            await self.reply(event, "登录成功但未获取到 Cookie（可能登录状态异常），请重新 #kg登录")
            return
        cookie = f"token={token};userid={userid}"
        try:
            self.plugin.config["defaultCookie"] = cookie
            self.plugin.config["defaultUid"] = str(userid)
            self.plugin.config.save_config()
            self.log_info("扫码登录成功，Cookie 已写入插件配置 defaultCookie")
        except Exception as e:
            self.log_warn(f"写入默认 Cookie 失败: {e}")
        await self.reply(
            event, f"✅ 登录成功：{nickname or userid or '已写入 Cookie'}\nCookie 已存入插件配置，全群默认使用该账号"
        )
        # 刷新登录 token 延长过期时间（fire-and-forget）
        try:
            asyncio.create_task(self.refresh_token_once())
        except Exception:
            pass
        await self.send_status(event, status_data=None)

    async def refresh_token_once(self):
        try:
            await kgapi.login_token_refresh()
            self.log_info("已刷新酷狗登录 token")
        except ApiError as e:
            self.log_warn(f"刷新登录 token 失败: {e}")

    async def build_status(self, *, status_data: dict | None = None) -> dict:
        cfg = self.cfg()
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
        uid = await self.get_uid()
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

    async def send_status(self, event: AstrMessageEvent, *, status_data: dict | None = None):
        try:
            status = await self.build_status(status_data=status_data)
            data = cardlib.build_status_card_data(status)
            await self.reply_card_or_text(
                event, tpl_name="kg-status", data=data, format_text=lambda d: cardlib.format_status_text(status)
            )
        except Exception as err:
            self.log_warn(f"状态卡片失败: {err}")
            await self.reply(
                event,
                cardlib.format_status_text(
                    {
                        "loggedIn": False,
                        "apiBase": self.cfg().get("apiBase") or "",
                        "quality": str(self.cfg().get("quality") or "auto"),
                        "keyStatus": str(err),
                    }
                ),
            )

    # ──────────── 分享解析 ────────────

    async def handle_resolve(self, event: AstrMessageEvent, text: str) -> bool:
        try:
            h = kgapi.extract_kugou_hash(text)
            if h and re.fullmatch(r"[0-9A-Fa-f]{32}", h):
                song = await kgapi.audio_by_hash(h.upper())
                if not song:
                    await self.reply(event, "该歌曲不存在或无版权")
                    return True
                await self.play_song(event, song, source="链接解析")
                return True
            if h:
                # 提取到的是 mixsongid（数字）：KuGouMusicApi 的 /audio 只支持
                # 32 位文件 hash，无法据此取链，明确提示而非静默失败
                await self.reply(event, "该链接为 mixsongid 形式，暂不支持自动解析，可用 #kg点歌 关键词 代替")
                return True
            # 无 hash：把链接去掉后当关键词搜索
            kw = re.sub(r"https?://\S+|\[CQ:[^\]]*\]", "", text).strip()
            kw = re.sub(r"kugou\.com|酷狗|分享|歌曲|链接", "", kw, flags=re.IGNORECASE).strip()
            if len(kw) >= 2:
                lst = await kgapi.search(kw, "song", pagesize=1)
                if lst:
                    await self.play_song(event, lst[0], source="链接解析")
                    return True
            await self.reply(event, "无法解析该酷狗链接（暂支持 hash 歌曲链接）")
            return True
        except ApiError as err:
            self.log_warn(f"解析失败: {err}")
            await self.reply(event, f"解析失败：{err}")
            return True
