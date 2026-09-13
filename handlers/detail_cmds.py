from __future__ import annotations

import re
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from ..core.service import MusicService

from ..core import api as kgapi
from ..core import cards as cardlib
from ..core.api import ApiError
from ..core.messages import NOT_FOUND, NOT_FOUND_ALBUM, NOT_FOUND_PLAYLIST
from .base import Route


async def get_lyric(service: MusicService, event: AstrMessageEvent):
    """#kg歌词 [关键词]：先选歌（回复 #kg听N）再显示歌词"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*歌词\s*(.*)$")
    if not m:
        return
    await service.start_select(event, "lyric", m.group(1).strip(), label="歌词", verb="查看歌词")
    event.stop_event()


async def lyric_word(service: MusicService, event: AstrMessageEvent):
    """#kg逐字歌词 [关键词]：先选歌（回复 #kg听N）再显示 KRC 逐字歌词"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*逐字歌词\s*(.*)$")
    if not m:
        return
    await service.start_select(event, "lyric_word", m.group(1).strip(), label="逐字歌词", verb="查看逐字歌词")
    event.stop_event()


async def get_comment(service: MusicService, event: AstrMessageEvent):
    """#kg评论 [关键词]：先选歌（回复 #kg听N）再显示歌曲热评"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*评论\s*(.*)$")
    if not m:
        return
    await service.start_select(event, "comment", m.group(1).strip(), label="评论", verb="查看评论")
    event.stop_event()


async def mv(service: MusicService, event: AstrMessageEvent):
    """#kgMV [关键词]：先选歌（回复 #kg听N）再查看 MV 详情与播放链接"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*MV\s*(.*)$")
    if not m:
        return
    await service.start_select(event, "mv", m.group(1).strip(), label="MV", verb="查看MV")
    event.stop_event()


async def climax(service: MusicService, event: AstrMessageEvent):
    """#kg高潮 关键词：歌曲高潮片段时间"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*高潮\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        song = await service.resolve_song(kw)
        if not song:
            await service.reply(event, NOT_FOUND.format(kw))
            event.stop_event()
            return
        c = await kgapi.song_climax(song.get("hash") or "")
        if not c.get("start_ms"):
            await service.reply(event, f"「{song.get('name') or ''}」暂无高潮数据")
            event.stop_event()
            return
        def fmt(ms: int) -> str:
            return f"{ms // 60000:02d}:{(ms % 60000) // 1000:02d}"

        await service.reply(
            event,
            f"🎯 高潮片段：{fmt(c['start_ms'])} - {fmt(c['end_ms'])}（约 {c['duration_ms'] // 1000} 秒）\n"
            f"♪ {song.get('name') or ''} - {song.get('artist') or ''}",
        )
    except ApiError as err:
        service.log_warn(f"高潮失败: {err}")
        await service.reply(event, f"获取高潮失败：{err}")
    event.stop_event()


async def ai_recommend_cmd(service: MusicService, event: AstrMessageEvent):
    """#kgAI推荐 关键词：AI 相似歌曲推荐"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*(?:AI|ai)推荐\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        song = await service.resolve_song(kw)
        if not song:
            await service.reply(event, NOT_FOUND.format(kw))
            event.stop_event()
            return
        mix = song.get("mixsongid") or song.get("id") or ""
        songs = await kgapi.ai_recommend(mix)
        if not songs:
            await service.reply(event, "暂无 AI 推荐歌曲")
            event.stop_event()
            return
        await service.list_to_session(event, f"AI 推荐 · {song.get('name') or ''}", songs)
    except ApiError as err:
        service.log_warn(f"AI推荐失败: {err}")
        await service.reply(event, f"获取 AI 推荐失败：{err}")
    event.stop_event()


async def favorite_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg收藏 [关键词]：先选歌（回复 #kg听N）再查看歌曲收藏数"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*收藏\s*(.*)$")
    if not m:
        return
    await service.start_select(event, "favorite", m.group(1).strip(), label="收藏", verb="查看收藏数")
    event.stop_event()


async def song_versions(service: MusicService, event: AstrMessageEvent):
    """#kg版本 [关键词]：先选歌（回复 #kg听N）再查看同一首歌的其他版本"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*(?:版本|相似)\s*(.*)$")
    if not m:
        return
    await service.start_select(event, "versions", m.group(1).strip(), label="版本", verb="查看其他版本")
    event.stop_event()


async def playlist_comment(service: MusicService, event: AstrMessageEvent):
    """#kg歌单评论 关键词|id：歌单热评"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*歌单评论\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        p = await service.resolve_playlist(kw)
        if not p:
            await service.reply(event, NOT_FOUND_PLAYLIST.format(kw))
            event.stop_event()
            return
        comments = await kgapi.comment_playlist(p.get("id"))
        if not comments:
            await service.reply(event, "该歌单暂无评论")
            event.stop_event()
            return
        data = cardlib.build_comment_card_data(p, comments, total=len(comments))
        await service.reply_card_or_text(
            event,
            tpl_name="kg-comment",
            data=data,
            format_text=lambda d: cardlib.format_comment_text(p, comments),
        )
    except ApiError as err:
        service.log_warn(f"歌单评论失败: {err}")
        await service.reply(event, f"获取歌单评论失败：{err}")
    event.stop_event()


async def album_comment(service: MusicService, event: AstrMessageEvent):
    """#kg专辑评论 专辑：专辑热评"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*专辑评论\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        a = await service.resolve_album(kw)
        if not a:
            await service.reply(event, NOT_FOUND_ALBUM.format(kw))
            event.stop_event()
            return
        comments = await kgapi.comment_album(a.get("id"))
        if not comments:
            await service.reply(event, "该专辑暂无评论")
            event.stop_event()
            return
        data = cardlib.build_comment_card_data(a, comments, total=len(comments))
        await service.reply_card_or_text(
            event,
            tpl_name="kg-comment",
            data=data,
            format_text=lambda d: cardlib.format_comment_text(a, comments),
        )
    except ApiError as err:
        service.log_warn(f"专辑评论失败: {err}")
        await service.reply(event, f"获取专辑评论失败：{err}")
    event.stop_event()


async def comment_count_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg评论数 关键词：歌曲评论数"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*评论数\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        song = await service.resolve_song(kw)
        if not song:
            await service.reply(event, NOT_FOUND.format(kw))
            event.stop_event()
            return
        cnt = await kgapi.comment_count(song.get("hash") or "")
        await service.reply(
            event,
            f"💬 评论数：{cardlib.fmt_count(cnt) if cnt else '未知'}\n♪ {song.get('name') or ''} - {song.get('artist') or ''}",
        )
    except ApiError as err:
        service.log_warn(f"评论数失败: {err}")
        await service.reply(event, f"获取评论数失败：{err}")
    event.stop_event()


ROUTES = [
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*歌词\s*(.*)$", re.IGNORECASE),
        name="get_lyric",
        doc="#kg歌词 [关键词]：先选歌（回复 #kg听N）再显示歌词",
        run=get_lyric,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*逐字歌词\s*(.*)$", re.IGNORECASE),
        name="lyric_word",
        doc="#kg逐字歌词 [关键词]：先选歌（回复 #kg听N）再显示 KRC 逐字歌词",
        run=lyric_word,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*评论\s*(.*)$", re.IGNORECASE),
        name="get_comment",
        doc="#kg评论 [关键词]：先选歌（回复 #kg听N）再显示歌曲热评",
        run=get_comment,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*MV\s*(.*)$", re.IGNORECASE),
        name="mv",
        doc="#kgMV [关键词]：先选歌（回复 #kg听N）再查看 MV 详情与播放链接",
        run=mv,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*高潮\s+(.+)$", re.IGNORECASE),
        name="climax",
        doc="#kg高潮 关键词：歌曲高潮片段时间",
        run=climax,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*(?:AI|ai)推荐\s+(.+)$", re.IGNORECASE),
        name="ai_recommend_cmd",
        doc="#kgAI推荐 关键词：AI 相似歌曲推荐",
        run=ai_recommend_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*收藏\s*(.*)$", re.IGNORECASE),
        name="favorite_cmd",
        doc="#kg收藏 [关键词]：先选歌（回复 #kg听N）再查看歌曲收藏数",
        run=favorite_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*(?:版本|相似)\s*(.*)$", re.IGNORECASE),
        name="song_versions",
        doc="#kg版本 [关键词]：先选歌（回复 #kg听N）再查看同一首歌的其他版本",
        run=song_versions,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*歌单评论\s+(.+)$", re.IGNORECASE),
        name="playlist_comment",
        doc="#kg歌单评论 关键词|id：歌单热评",
        run=playlist_comment,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*专辑评论\s+(.+)$", re.IGNORECASE),
        name="album_comment",
        doc="#kg专辑评论 专辑：专辑热评",
        run=album_comment,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*评论数\s+(.+)$", re.IGNORECASE),
        name="comment_count_cmd",
        doc="#kg评论数 关键词：歌曲评论数",
        run=comment_count_cmd,
        # 必须高于 #kg评论（^…评论\s*(.*)$ 会把「数 关键词」吃成关键词并 stop_event）
        priority=1,
    ),
]
