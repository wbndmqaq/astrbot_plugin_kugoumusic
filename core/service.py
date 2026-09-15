from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain

if TYPE_CHECKING:
    from ..main import KugouMusicPlugin

from . import api as kgapi
from . import cards as cardlib
from . import login as loginflow
from . import panels as panelmod
from .api import ApiError, cfg_int
from .delivery import (
    _schedule_cleanup,
    _write_bytes,
    cancel_cleanups,
    deliver_song,
    get_temp_dir,
)
from .messages import LYRICS_SOURCE, NEED_LOGIN, NOT_FOUND
from .quality import QUALITY_LABEL, trial_label
from .render import render_card_png

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)
PLUGIN_NAME = "astrbot_plugin_kugoumusic"

# #kg听所有 连播上限（防止误触发刷屏/大量下载）
PLAY_ALL_LIMIT = 30
# 歌词卡片每页最多行数（超出分页成多张卡片）
LYRIC_LINES_PER_PAGE = 36

NEW_ALBUM_AREAS = {"": 0, "推荐": 0, "华语": 1, "欧美": 2, "日本": 3, "韩国": 4}
GOOD_SONG_CARDS = {"精选": 1, "怀旧": 2, "热门": 3, "小众": 4, "vip": 6, "VIP": 6}
ARTIST_LIST_TYPES = {"": 0, "全部": 0, "华语": 1, "欧美": 2, "日韩": 3, "其他": 4, "日本": 5, "韩国": 6}


def _owner_marker_path() -> Path:
    """三个音乐插件（网易云/酷狗/QQ）共用的「最近活跃归属」标记文件路径。

    用于裸 #听N 的跨插件抢占：点歌出列表时写入本插件名，裸 #听N 仅由最近
    活跃的插件响应，避免多插件同装时抢占顺序取决于插件加载顺序。
    """
    try:
        from astrbot.api.star import StarTools

        # 必须显式传插件名：StarTools 只在调用栈位于插件主模块（main.py）时才能
        # 反查插件元数据，本函数在 core/service.py 里，不传名会抛 RuntimeError。
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        return data_dir.parent / "_music_session_owner.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "_music_session_owner.json"


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
        # fire-and-forget 后台任务集合：持有引用避免被 GC，卸载时统一取消
        self._bg_tasks: set[asyncio.Task] = set()
        # 临时文件清理定时器不再由实例持有：统一登记在 core/delivery.py 的模块级注册表
        # （{句柄: 路径}），terminate() 调一次 cancel_cleanups() 即可取消**并补删**，
        # 不会出现「实例表 + 模块表各管一半」的漏清。
        # 注入配置访问器给 api 模块
        kgapi.set_config_getter(lambda: self.plugin.config or {})

    # ──────────── 裸 #听N 跨插件抢占 ────────────

    async def mark_session_owner(self) -> None:
        """点歌出列表后，把本插件记录为「最近活跃的音乐插件」。"""
        name = str(getattr(self.plugin, "name", "") or "")

        def _w():
            try:
                p = _owner_marker_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(
                    json.dumps({"plugin": name, "ts": int(time.time())}, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(tmp, p)  # 原子替换，避免并发写坏
            except Exception:
                pass

        await asyncio.to_thread(_w)

    async def is_session_owner(self) -> bool:
        """本插件是否为最近活跃的音乐插件（无标记时视为 True，退化为「谁有会话谁响应」）。"""
        name = str(getattr(self.plugin, "name", "") or "")

        def _r() -> bool:
            try:
                p = _owner_marker_path()
                if not p.exists():
                    return True
                data = json.loads(p.read_text(encoding="utf-8"))
                return data.get("plugin") == name
            except Exception:
                return True

        return await asyncio.to_thread(_r)

    # ──────────── 生命周期 ────────────

    async def initialize(self):
        """启动初始化：设备 dfid 注册 + ffmpeg 路径预热（把阻塞 IO 挪出首次投递）。"""
        try:
            await self.ensure_device()
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"初始化设备 Cookie 失败: {e}")
        from .delivery import probe_ffmpeg_path

        try:
            await asyncio.to_thread(probe_ffmpeg_path)
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"ffmpeg 预热失败: {e}")

    async def ensure_device(self):
        from astrbot.api.star import StarTools

        # 显式传插件名：本方法在 core/service.py，StarTools 无法从调用栈反查
        # 插件元数据，不传名会抛 RuntimeError 并被 initialize 吞掉，dfid 永不注册。
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        # 建目录与读写设备 Cookie 都是阻塞 IO，统一丢线程池：
        # 这是 initialize() 里唯一的磁盘操作，不该占用事件循环。
        try:
            await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
        except Exception:
            pass
        dev_file = data_dir / "device_cookies.json"
        try:
            if await asyncio.to_thread(dev_file.exists):
                raw = json.loads(await asyncio.to_thread(dev_file.read_text, "utf-8"))
                if raw.get("cookie"):
                    kgapi.set_device_cookie(str(raw["cookie"]))
                    return
        except Exception:
            pass
        dfid = await kgapi.register_dev()
        if dfid:
            try:
                await asyncio.to_thread(
                    dev_file.write_text,
                    json.dumps(
                        {"cookie": f"dfid={dfid}", "ts": int(time.time())}, ensure_ascii=False
                    ),
                    "utf-8",
                )
                self.log_info(f"已注册酷狗设备 dfid（{dfid[:6]}…）")
            except Exception as e:
                self.log_warn(f"持久化设备 Cookie 失败: {e}")

    async def terminate(self):
        # 顺序：先停「会用到会话/浏览器的活动」，再关资源，最后处理清理登记。
        # 原顺序先关 aiohttp 会话、后取消后台任务：取消前的瞬间后台任务（上报
        # 听歌历史等）可能正拿着已关闭的会话发请求而报错。
        for user_key in list(self.active_logins.keys()):
            self.stop_poll(user_key)
        # 取消所有 fire-and-forget 后台任务（上报听歌历史、刷新 token 等），
        # 并等待其退出后再关会话——cancel() 只是发出请求，被取消的任务可能正拿
        # 着 aiohttp 会话在 await 中，不等它落定就 close_session() 会留下
        # 「会话已关闭仍被引用」的窗口报错（与 neteasemusic 同口径）。
        tasks = list(self._bg_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 取消待执行的临时文件清理定时器，并 best-effort 删除其登记的残留文件
        # （只 cancel 不删会让已排期的音频/卡片图永久留在 tempDir，重载越频繁残留越多；
        # cancel_cleanups 内部带 5 秒宽限期，不会误删「已登记、发送方仍在读盘」的文件）
        cancel_cleanups()
        # 关闭复用的 aiohttp 会话，避免热重载后残留连接
        await kgapi.close_session()
        # 关闭常驻的 Chromium 实例，避免热重载后残留浏览器进程（渲染环境缺失时静默跳过）
        from .render import close_browser

        try:
            await close_browser()
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"关闭渲染浏览器失败: {e}")

    async def save_config(self) -> bool:
        """安全写盘：兼容旧版 AstrBot（无异步写盘 API）。返回是否写入成功。"""
        cfg = self.plugin.config
        try:
            saver = getattr(cfg, "save_config_async", None)
            if callable(saver):
                # AstrBot 4.27.4 起 save_config_async() 返回「本次快照是否被提交」：
                # 并发写盘时有更新的快照先落地则本次返回 False（内容没写进文件）。
                # 必须返回真实结果，否则又变成「写盘失败却谎报成功」——三处调用点
                # （扫码登录 / 音质 / api）正是靠这个 bool 决定是否提示写盘失败。
                # 旧版无返回值（None）表示「无此语义」，按成功处理，避免把旧版误判为失败。
                committed = await saver()
                return committed is None or bool(committed)
            sync_saver = getattr(cfg, "save_config", None)
            if callable(sync_saver):
                # 同步写盘 API 无返回值语义（None），执行未抛异常即视为成功
                await asyncio.to_thread(sync_saver)
                return True
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"配置保存失败: {e}")
            return False
        self.log_warn("配置保存失败: 当前 AstrBot 版本不支持配置写盘 API")
        return False

    def _spawn_bg(self, coro):
        """后台任务：持有引用避免被 GC，并记录异常，插件卸载时统一取消。"""
        try:
            task = asyncio.create_task(coro)
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"后台任务启动失败: {e}")
            return None
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task):
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.log_warn(f"后台任务异常: {t.exception()}")

        task.add_done_callback(_done)
        return task

    # ──────────── 辅助 ────────────

    def cfg(self) -> dict:
        return self.plugin.config or {}

    def log_warn(self, msg: str):
        logger.warning(f"[kugou] {msg}")

    def log_info(self, msg: str):
        logger.info(f"[kugou] {msg}")

    def plain(self, text: str) -> Plain:
        return Plain(text=text)

    async def send_chain(self, event: AstrMessageEvent, *components, raise_on_error: bool = False) -> bool:
        """发送消息链；返回是否真的发出。

        ``raise_on_error=True`` 会把发送失败重新抛出：投递层（core/delivery.py）依赖
        这个异常触发降级链——语音直发失败时退回 `Record` 组件、文件发送失败时用
        ffmpeg 压成紧凑 mp3 重试。把异常吞掉，整条降级链就都成了死代码。

        默认 ``False``：只记日志并返回 ``False``，避免平台侧发送失败（风控/超限/协议
        错误）穿透成内核的通用报错——那种情况下用户看到的是「调用插件时出现异常」，
        而且 handler 里的 ``stop_event()`` 也不会执行。调用方按返回值决定是否降级。
        """
        comps = [c for c in components if c is not None]
        if not comps:
            return False
        # 先探测能力，而不是捕获 AttributeError：把「内核太旧、没有 event.send」与
        # 「平台适配器内部抛 AttributeError」区分开，后者应被当成发送失败。
        if getattr(event, "send", None) is None:
            # 事件对象根本没有 send（极旧内核）：退无可退，直接按发送失败处理。
            # 注意这里**不能**再调 event.send 重发文本——它按定义就是 None，
            # 只会抛 AttributeError 掩盖真正的原因。
            texts = [str(t) for t in (getattr(_c, "text", None) for _c in comps) if t]
            self.log_warn(
                "_send_chain：事件对象没有 send 方法，无法发送"
                + (f"（原有文本 {len(texts)} 段）" if texts else "")
            )
            if raise_on_error:
                raise RuntimeError("event.send 不可用，无法发送消息")
            return False
        mc = MessageChain(chain=list(comps))
        mc.use_markdown_ = False
        try:
            await event.send(mc)
            return True
        except Exception as err:  # noqa: BLE001
            self.log_warn(f"_send_chain 发送失败: {err}")
            if raise_on_error:
                raise
            return False

    async def reply(self, event: AstrMessageEvent, text: str):
        try:
            await self.send_chain(event, self.plain(text))
        except Exception as e:  # noqa: BLE001
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

    async def song_by_hash(self, kw: str) -> dict | None:
        """按 32 位 hex 文件 hash 直查歌曲详情；非 hash 形或查不到返回 None。

        总超时（``ApiError.timeout``）不吞：吞掉会让调用方立刻退回**必然同样超时**
        的关键词搜索，把一次 20 秒超时放大成两次（#kg听所有 串行时更明显）。
        """
        kw = (kw or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", kw):
            return None
        try:
            return await kgapi.audio_by_hash(kw.upper()) or None
        except ApiError as e:
            if e.timeout:
                raise
            return None

    async def resolve_song(self, kw: str) -> dict | None:
        kw = (kw or "").strip()
        if not kw:
            return None
        # 32 位 hex = 酷狗文件 hash，直接查详情
        s = await self.song_by_hash(kw)
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

        ``kw`` 也接受 32 位 hex 歌曲 hash（帮助卡片写「关键词|hash」）：先按 hash
        直查详情得到唯一候选，查不到再退回关键词搜索，避免把 hash 当关键词搜。
        """
        scope = self.scope(event)
        session = await cardlib.SessionStore.get(self.plugin, scope)
        kw = (kw or "").strip()
        if kw:
            try:
                song = await self.song_by_hash(kw)
            except ApiError as err:
                # 总超时（hash 直查 20 秒没回）：不要退回关键词搜索再等一轮超时
                self.log_warn(f"选歌 hash 直查失败: {err}")
                await self.reply(event, f"点歌失败：{err}")
                return
            if song:
                lst = [song]
                keyword = song.get("name") or kw
            else:
                page_size = min(cfg_int(self.cfg(), "maxList", 10), 20)
                try:
                    lst = await kgapi.search(kw, "song", pagesize=page_size)
                except ApiError as err:
                    # 搜索失败必须给回复：原先 ApiError 直接冒泡出 handler，用户什么都没收到
                    self.log_warn(f"选歌搜索失败: {err}")
                    await self.reply(event, f"点歌失败：{err}")
                    return
                if not lst:
                    await self.reply(event, NOT_FOUND.format(kw))
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
        await self.mark_session_owner()
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
        await self.send_lyric_pages(event, song, lines, base_tip=LYRICS_SOURCE)

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
        pages = [lines[i : i + LYRIC_LINES_PER_PAGE] for i in range(0, len(lines), LYRIC_LINES_PER_PAGE)]
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
            await self.reply(event, NEED_LOGIN)
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
            res = await deliver_song(self.plugin, event, song, play, cfg=cfg)
            # 双通道都没发出（如平台无语音且未开文件）时不该上报听歌历史
            if res.get("ok"):
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

        self._spawn_bg(_do())

    async def list_to_session(self, event: AstrMessageEvent, keyword: str, songs: list, *, tip: str = "") -> bool:
        scope = self.scope(event)
        await cardlib.SessionStore.set(self.plugin, scope, {"type": "kg_songs", "keyword": keyword, "data": songs})
        await self.mark_session_owner()
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
            d = await get_temp_dir()
            file_path = os.path.join(d, f"card_{tpl_name}_{int(time.time() * 1000)}.png")
            await asyncio.to_thread(_write_bytes, file_path, raw)
            return file_path
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"{tpl_name} 本地渲染失败: {e}")
            return None

    async def reply_card_or_text(self, event: AstrMessageEvent, *, tpl_name: str, data: dict, format_text) -> bool:
        card_path = None
        try:
            card_path = await self.render_card(event, data, tpl_name)
            # 必须看发送结果：卡片被平台拒绝时要落到下面的纯文本兜底
            if card_path and await self.send_chain(event, Image.fromFileSystem(card_path)):
                return True
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"{tpl_name} 卡片渲染失败，回退文本: {e}")
        finally:
            if card_path:
                self.schedule_unlink(card_path)
        try:
            text = format_text(data)
            if text and await self.send_chain(event, self.plain(text)):
                return True
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"{tpl_name} 文本兜底失败: {e}")
        return False

    async def save_qr_image(self, b64: str) -> str | None:
        try:
            import base64

            raw = b64
            if "," in raw and raw.split(",", 1)[0].startswith("data:"):
                raw = raw.split(",", 1)[1]
            data = base64.b64decode(raw)
            path = os.path.join(await get_temp_dir(), f"qr_{int(time.time() * 1000)}.png")
            await asyncio.to_thread(_write_bytes, path, data)
            return path
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"保存二维码失败: {e}")
            return None

    def schedule_unlink(self, path: str, keep_sec: int | None = None) -> None:
        """延时清理临时文件（卡片图 / 二维码等）。

        延迟取 ``max(keepFileSec, MIN_CLEANUP_DELAY_SEC)``：``keepFileSec=0`` 也不能立即删
        （``event.send()`` 返回时协议端可能仍在读盘）。

        统一委托 ``core/delivery._schedule_cleanup``（模块级注册表 ``{句柄: 路径}``）：
        音频临时文件与卡片图/二维码共用一个登记表，``terminate()`` 只调一次
        ``cancel_cleanups()`` 即可取消**并补删**全部排期，避免两份注册表各清一半。
        """
        if not path:
            return
        if keep_sec is None:
            # 取值口径与 delivery 一致：None/空串（WebUI 清空）回落 schema 默认 60，
            # 显式 0 保留「尽快删除」语义（实际延迟由 MIN_CLEANUP_DELAY_SEC 兜底）。
            keep_sec = cfg_int(self.cfg(), "keepFileSec", 60)
        _schedule_cleanup(path, keep_sec)

    async def _lyric_content(self, song: dict, fmt: str) -> str:
        """按 hash 搜索歌词候选并取第一条的解码内容（lrc/krc 共用的候选逻辑）。"""
        candidates = await kgapi.lyric_search(hash_=song.get("hash") or "", album_audio_id=song.get("mixsongid") or "")
        if not candidates:
            return ""
        cand = candidates[0]
        if not cand.get("id") or not cand.get("accesskey"):
            return ""
        lr = await kgapi.lyric(cand["id"], cand["accesskey"], fmt=fmt)
        return lr.get("decodeContent") or ""

    async def fetch_lyric(self, song: dict) -> list:
        """按 hash 搜索歌词候选并取第一条，返回纯文本行列表。"""
        content = await self._lyric_content(song, "lrc")
        if not content:
            return []
        return self.extract_lyric_lines(content)

    @staticmethod
    def extract_lyric_lines(lrc: str) -> list:
        """LRC 文本 → 纯文本行，完整返回（分页由调用方处理）。"""
        def _strip_meta(lines):
            return [
                line
                for line in lines
                if not re.match(r"^\s*\[(ti|ar|al|by|offset|total):", line, re.IGNORECASE)
            ]

        out = []
        for line in _strip_meta(lrc.splitlines()):
            t = re.sub(r"^\[[^\]]*\]", "", line).strip()
            if t:
                out.append(t)
        return out

    async def fetch_krc_lyric(self, song: dict) -> list:
        """按 hash 取 KRC 逐字歌词，剥离 [时间] 与 <逐字> 标签返回文本行。"""
        content = await self._lyric_content(song, "krc")
        if not content:
            return []
        out = []
        for line in content.splitlines():
            t = re.sub(r"^\[[^\]]*\]", "", line)
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
            await self.save_config()
        return uid

    def stop_poll(self, user_key: str):
        """停止某用户的扫码登录轮询（薄委托：实现见 core/login.py）。"""
        loginflow.stop_poll(self, user_key)

    def start_poll(self, event: AstrMessageEvent, key: str, max_sec: int = 300):
        """轮询酷狗 App 扫码状态（薄委托：实现见 core/login.py）。"""
        loginflow.start_poll(self, event, key, max_sec)

    def start_qq_poll(self, event: AstrMessageEvent, qr_ctx: dict, max_sec: int = 180):
        """轮询 QQ 扫码状态（薄委托：实现见 core/login.py）。"""
        loginflow.start_qq_poll(self, event, qr_ctx, max_sec)

    async def refresh_token_once(self):
        """刷新登录 token（薄委托：实现见 core/login.py）。"""
        await loginflow.refresh_token_once(self)

    async def send_status(self, event: AstrMessageEvent, *, status_data: dict | None = None):
        """发送状态卡（薄委托：实现见 core/panels.py）。"""
        await panelmod.send_status(self, event, status_data=status_data)

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
