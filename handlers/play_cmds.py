from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from ..core.service import MusicService

from ..core import api as kgapi
from ..core import cards as cardlib
from ..core.api import ApiError, cfg_int
from ..core.delivery import deliver_song
from ..core.messages import INDEX_OUT_OF_RANGE, NOT_FOUND
from ..core.service import PLAY_ALL_LIMIT
from .base import Route


async def pick_song(service: MusicService, event: AstrMessageEvent):
    """#kg点歌 关键词：搜索并列出歌曲列表，发送 #kg听序号 播放"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*点歌\s*(.+)$", song_request=True)
    if not m:
        return
    keyword = m.group(1).strip()
    if not keyword:
        await service.reply(event, "用法：#kg点歌 关键词")
        event.stop_event()
        return
    try:
        await service.reply(event, f"正在搜索：{keyword}")
        page_size = max(1, min(cfg_int(service.cfg(), "maxList", 10), 20))
        lst = await kgapi.search(keyword, "song", pagesize=page_size)
        if not lst:
            await service.reply(event, "没有搜到相关歌曲")
            event.stop_event()
            return
        await service.list_to_session(event, keyword, lst)
    except ApiError as err:
        service.log_warn(f"点歌失败: {err}")
        await service.reply(event, f"点歌失败：{err}")
    event.stop_event()


async def choose_song(service: MusicService, event: AstrMessageEvent):
    """#kg听N：播放当前酷狗点歌列表第 N 首；若会话有待办动作（#kg歌词 等先选歌）则先执行该动作"""
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
        return
    m = re.match(
        r"^#?(?:kg|KG)\s*听\s*([1-9][0-9]?)$|^#?\s*听\s*([1-9][0-9]?)$", event.message_str.strip(), re.IGNORECASE
    )
    n = int(m.group(1) or m.group(2) or 0) if m else 0
    # 裸 #听N（无 kg 前缀）仅由最近活跃的音乐插件响应，避免多插件同装时抢占顺序取决于加载顺序
    if m and m.group(2) and not await service.is_session_owner():
        return
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope)
    # 会话必须是本插件（kg_songs），否则不抢其它插件的 #听
    if not session or session.get("type") != "kg_songs" or not session.get("data"):
        return
    songs = session.get("data") or []
    if n < 1 or n > len(songs):
        await service.reply(event, INDEX_OUT_OF_RANGE.format(len(songs)))
        event.stop_event()
        return
    song = songs[n - 1]
    action = session.get("action") or "play"
    # 待办动作一次性消费：先清掉 action，避免下次 #听N 误触发
    if action != "play":
        await cardlib.SessionStore.set(service.plugin, scope, {**session, "action": "play"})
    try:
        if action == "lyric":
            await service.show_lyric(event, song)
        elif action == "lyric_word":
            await service.show_lyric_word(event, song)
        elif action == "comment":
            await service.show_comment(event, song)
        elif action == "mv":
            await service.show_mv(event, song)
        elif action == "favorite":
            await service.show_favorite(event, song)
        elif action == "versions":
            await service.show_versions(event, song)
        else:
            await service.play_song(event, song, source="点歌")
    except ApiError as err:
        service.log_warn(f"执行失败: {err}")
        await service.reply(event, f"操作失败：{err}")
    event.stop_event()


async def play_all(service: MusicService, event: AstrMessageEvent):
    """#kg听所有：依次发送当前会话列表的全部歌曲（语音+文件，上限 30 首）"""
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
        return
    if not re.match(
        r"^#?(?:kg|KG)\s*听\s*所有$|^#\s*听\s*所有$",
        event.message_str.strip(),
        re.IGNORECASE,
    ):
        return
    # 裸 #听所有（无 kg 前缀）仅由最近活跃的音乐插件响应
    if not re.match(r"^#?(?:kg|KG)", event.message_str.strip(), re.IGNORECASE) and not await service.is_session_owner():
        return
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope)
    # 会话必须是本插件（kg_songs），否则不抢其它插件的 #听所有
    if not session or session.get("type") != "kg_songs" or not session.get("data"):
        return
    songs = session.get("data") or []
    batch = songs[:PLAY_ALL_LIMIT]
    title = session.get("keyword") or "当前列表"
    await service.reply(
        event,
        f"▶ 开始连播「{title}」共 {len(batch)} 首"
        + (f"（列表共 {len(songs)} 首，仅连播前 {PLAY_ALL_LIMIT} 首）" if len(songs) > len(batch) else "")
        + "，逐首下载发送需要一些时间…",
    )
    ok = fail = 0
    for i, song in enumerate(batch):
        try:
            play = await service.resolve_play(song, cfg)
            if not play.get("url"):
                fail += 1
                service.log_warn(f"连播 {i + 1}/{len(batch)} 无播放链: {song.get('name')}")
                continue
            # 连播不发详情卡/文案/音乐卡，只发语音+文件
            res = await deliver_song(
                service.plugin,
                event,
                song,
                play,
                cfg=cfg,
                options={"skipTextInfo": True, "skipNativeCard": True},
            )
            # 投递层会如实返回「语音/文件两条通道是否真的发出」；不看返回值会让
            # 「连播完成：成功 N 首」把没发出去的歌也计成成功。
            if res.get("ok"):
                ok += 1
                service.report_play_history(song)
            else:
                fail += 1
                service.log_warn(
                    f"连播 {i + 1}/{len(batch)} 投递失败: {song.get('name')}"
                    f"（{res.get('reason') or '投递未成功'}）"
                )
        except ApiError as err:
            fail += 1
            service.log_warn(f"连播 {i + 1}/{len(batch)} 失败: {err}")
        except Exception as err:  # 单曲失败不中断连播
            fail += 1
            service.log_warn(f"连播 {i + 1}/{len(batch)} 失败: {type(err).__name__}: {err}")
        if i < len(batch) - 1:
            await asyncio.sleep(1)
    await service.reply(event, f"连播完成：成功 {ok} 首，失败 {fail} 首")
    event.stop_event()


async def play_direct(service: MusicService, event: AstrMessageEvent):
    """#kg播放 关键词：搜索并直接播放第一首"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*播放\s*(.+)$", song_request=True)
    if not m:
        return
    keyword = m.group(1).strip()
    if not keyword:
        await service.reply(event, "用法：#kg播放 关键词")
        event.stop_event()
        return
    try:
        lst = await kgapi.search(keyword, "song", pagesize=1)
        if not lst:
            await service.reply(event, NOT_FOUND.format(keyword))
            event.stop_event()
            return
        await service.play_song(event, lst[0], source="搜索")
    except ApiError as err:
        service.log_warn(f"播放失败: {err}")
        await service.reply(event, f"播放失败：{err}")
    event.stop_event()


ROUTES = [
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*点歌\s*(.+)$", re.IGNORECASE),
        name="pick_song",
        doc="#kg点歌 关键词：搜索并列出歌曲列表，发送 #kg听序号 播放",
        run=pick_song,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*听\s*([1-9][0-9]?)$|^#?\s*听\s*([1-9][0-9]?)$", re.IGNORECASE),
        name="choose_song",
        doc="#kg听N：播放当前酷狗点歌列表第 N 首；若会话有待办动作（#kg歌词 等先选歌）则先执行该动作",
        run=choose_song,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*听\s*所有$|^#\s*听\s*所有$", re.IGNORECASE),
        name="play_all",
        doc="#kg听所有：依次发送当前会话列表的全部歌曲（语音+文件，上限 30 首）",
        run=play_all,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*播放\s*(.+)$", re.IGNORECASE),
        name="play_direct",
        doc="#kg播放 关键词：搜索并直接播放第一首",
        run=play_direct,
    ),
]
