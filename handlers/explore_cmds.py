from __future__ import annotations

import re
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from ..core.service import MusicService

from ..core import api as kgapi
from ..core import cards as cardlib
from ..core.api import ApiError
from ..core.messages import (
    INDEX_OUT_OF_RANGE,
    NOT_FOUND_ALBUM,
    NOT_FOUND_PLAYLIST,
    NOT_FOUND_SINGER,
    TIP_ALBUM_TRACKS,
    TIP_HISTORY,
    TIP_RANK_BY_NAME,
    TIP_RANK_LIST,
    TIP_SINGER_HOT,
    TIP_THEME_PLAYLIST,
    TIP_YUEKU,
)
from ..core.service import ARTIST_LIST_TYPES, GOOD_SONG_CARDS, NEW_ALBUM_AREAS
from .base import Route


async def chart(service: MusicService, event: AstrMessageEvent):
    """#kg排行 [榜单名]：查看排行榜列表或具体榜单歌曲"""
    if not service.cfg().get("enable", True):
        return
    m = re.match(r"^#?(?:kg|KG)\s*排行\s*(.*)$", event.message_str.strip(), re.IGNORECASE)
    name = (m.group(1).strip() if m else "").strip()
    try:
        tops = await kgapi.rank_list()
        if not tops:
            await service.reply(event, "暂无榜单数据")
            event.stop_event()
            return
        if not name:
            # 榜单太多，展示前 30 个，完整列表仍支持名称匹配
            shown = tops[:30]
            items = [
                {"name": t.get("name") or "", "sub": f"更新 {t.get('update')}s" if t.get("update") else ""}
                for t in shown
            ]
            data, format_text = _generic_panel(
                "酷狗排行榜",
                items,
                subtitle=f"共 {len(tops)} 个榜单，显示前 {len(shown)} 个",
                tip=TIP_RANK_LIST,
                cfg=service.cfg(),
            )
            await service.reply_card_or_text(
                event, tpl_name="kg-generic", data=data, format_text=format_text
            )
            event.stop_event()
            return
        target = None
        for t in tops:
            t_name = str(t.get("name") or "")
            if name == str(t.get("id") or "") or (t_name and (name in t_name or t_name in name)):
                target = t
                break
        if not target:
            await service.reply(event, f"未找到榜单「{name}」，发送 #kg排行 查看全部榜单")
            event.stop_event()
            return
        songs = await kgapi.rank_audio(target["id"], pagesize=60)
        if not songs:
            await service.reply(event, f"榜单「{target.get('name') or ''}」暂无数据")
            event.stop_event()
            return
        await service.list_to_session(event, f"排行榜 · {target.get('name') or ''}", songs)
    except ApiError as err:
        service.log_warn(f"排行失败: {err}")
        await service.reply(event, f"获取排行榜失败：{err}")
    event.stop_event()


async def artist(service: MusicService, event: AstrMessageEvent):
    """#kg歌手 关键词：查看歌手热门歌曲"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*歌手\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        a = await service.resolve_artist(kw)
        if not a:
            await service.reply(event, NOT_FOUND_SINGER.format(kw))
            event.stop_event()
            return
        songs = await kgapi.artist_audios(a.get("id"), sort="hot", pagesize=30)
        if not songs:
            await service.reply(event, f"歌手「{a.get('name') or ''}」暂无热门歌曲")
            event.stop_event()
            return
        await service.list_to_session(event, f"歌手 · {a.get('name') or ''}", songs)
    except ApiError as err:
        service.log_warn(f"歌手失败: {err}")
        await service.reply(event, f"获取歌手歌曲失败：{err}")
    event.stop_event()


async def album(service: MusicService, event: AstrMessageEvent):
    """#kg专辑 关键词：查看专辑曲目"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*专辑\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        a = await service.resolve_album(kw)
        if not a:
            await service.reply(event, NOT_FOUND_ALBUM.format(kw))
            event.stop_event()
            return
        songs = await kgapi.album_songs(a.get("id"), pagesize=30)
        if not songs:
            await service.reply(event, f"专辑「{a.get('name') or ''}」暂无曲目")
            event.stop_event()
            return
        await service.list_to_session(
            event, f"专辑 · {a.get('name') or ''}", songs, tip=f"歌手：{a.get('artist') or ''} · 共 {len(songs)} 首"
        )
    except ApiError as err:
        service.log_warn(f"专辑失败: {err}")
        await service.reply(event, f"获取专辑失败：{err}")
    event.stop_event()


async def playlist(service: MusicService, event: AstrMessageEvent):
    """#kg歌单 关键词|id：搜索歌单并查看曲目（VIP 歌单需登录）"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*歌单\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        p = await service.resolve_playlist(kw)
        if not p:
            await service.reply(event, NOT_FOUND_PLAYLIST.format(kw))
            event.stop_event()
            return
        try:
            songs = await kgapi.playlist_tracks(p.get("id"), pagesize=100)
        except ApiError as e:
            if e.code in (20010, 20017):
                await service.reply(event, f"获取歌单「{p.get('name') or kw}」曲目需要登录：{e}")
                event.stop_event()
                return
            raise
        if not songs:
            await service.reply(event, f"歌单「{p.get('name') or kw}」暂无曲目或需要登录")
            event.stop_event()
            return
        shown = songs[:30]
        await service.list_to_session(
            event, f"歌单 · {p.get('name') or ''}", shown, tip=f"歌单共 {len(songs)} 首，显示前 {len(shown)} 首"
        )
    except ApiError as err:
        service.log_warn(f"歌单失败: {err}")
        await service.reply(event, f"获取歌单失败：{err}")
    event.stop_event()


async def new_song(service: MusicService, event: AstrMessageEvent):
    """#kg新歌：新歌速递"""
    if not service.cfg().get("enable", True):
        return
    try:
        songs = await kgapi.top_song(rank_id=21608, pagesize=30)
        if not songs:
            await service.reply(event, "暂无新歌数据")
            event.stop_event()
            return
        await service.list_to_session(event, "新歌速递", songs[:20])
    except ApiError as err:
        service.log_warn(f"新歌失败: {err}")
        await service.reply(event, f"获取新歌失败：{err}")
    event.stop_event()


async def top_playlist(service: MusicService, event: AstrMessageEvent):
    """#kg精品歌单：精选歌单"""
    if not service.cfg().get("enable", True):
        return
    try:
        pls = await kgapi.top_playlists(pagesize=15)
        if not pls:
            await service.reply(event, "暂无精品歌单数据")
            event.stop_event()
            return
        data = cardlib.build_playlist_card_data("精品歌单", pls, subtitle="精选歌单推荐", cfg=service.cfg())
        await service.reply_card_or_text(
            event,
            tpl_name="kg-playlist",
            data=data,
            format_text=lambda d: cardlib.format_playlist_text("精品歌单", pls),
        )
    except ApiError as err:
        service.log_warn(f"精品歌单失败: {err}")
        await service.reply(event, f"获取精品歌单失败：{err}")
    event.stop_event()


async def catlist(service: MusicService, event: AstrMessageEvent):
    """#kg歌单分类：歌单分类列表"""
    if not service.cfg().get("enable", True):
        return
    try:
        cats = await kgapi.playlist_tags()
        if not cats:
            await service.reply(event, "暂无歌单分类数据")
            event.stop_event()
            return
        items = [
            {"name": c.get("name") or "", "tag": f"{cardlib.fmt_count(c.get('count'))}" if c.get("count") else ""}
            for c in cats[:40]
        ]
        data, format_text = _generic_panel(
            "歌单分类", items, subtitle="歌单标签分类", cfg=service.cfg()
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"歌单分类失败: {err}")
        await service.reply(event, f"获取歌单分类失败：{err}")
    event.stop_event()


async def suggest(service: MusicService, event: AstrMessageEvent):
    """#kg搜索建议 关键词：关键词补全"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*搜索建议\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        items = await kgapi.search_suggest(kw)
        if not items:
            await service.reply(event, "暂无补全建议")
        else:
            rows = [{"name": w} for w in items]
            data, format_text = _generic_panel(
                f"「{kw}」的搜索建议", rows, subtitle="关键词补全", cfg=service.cfg()
            )
            await service.reply_card_or_text(
                event, tpl_name="kg-generic", data=data, format_text=format_text
            )
    except ApiError as err:
        service.log_warn(f"搜索建议失败: {err}")
        await service.reply(event, f"获取搜索建议失败：{err}")
    event.stop_event()


async def new_album(service: MusicService, event: AstrMessageEvent):
    """#kg新碟 [华语/欧美/日本/韩国]：新碟上架"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*新碟\s*(.*)$")
    if not m:
        return
    area = m.group(1).strip()
    area_id = NEW_ALBUM_AREAS.get(area, 0)
    try:
        albums = await kgapi.top_album(area_id, pagesize=15)
        if not albums:
            await service.reply(event, "暂无新碟数据")
            event.stop_event()
            return
        rows = [
            {
                "name": a.get("name") or "",
                "sub": a.get("artist") or "",
                "tag": a.get("publishDate") or "",
                "cover": a.get("cover") or "",
            }
            for a in albums
        ]
        # 文本兜底只展示 名称+歌手（不显示上架日期），按同一份 albums 投影一遍
        text_rows = [{"name": a.get("name") or "", "sub": a.get("artist") or ""} for a in albums]
        data, format_text = _generic_panel(
            f"新碟上架 · {area or '推荐'}",
            rows,
            subtitle="最新专辑",
            tip=TIP_ALBUM_TRACKS,
            cfg=service.cfg(),
            text_rows=text_rows,
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"新碟失败: {err}")
        await service.reply(event, f"获取新碟失败：{err}")
    event.stop_event()


async def good_song(service: MusicService, event: AstrMessageEvent):
    """#kg好歌 [精选/怀旧/热门/小众/VIP]：好歌精选卡片"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*好歌\s*(.*)$")
    if not m:
        return
    card = m.group(1).strip()
    card_id = GOOD_SONG_CARDS.get(card, 3)
    try:
        songs = await kgapi.top_card(card_id, pagesize=20)
        if not songs:
            await service.reply(event, "暂无推荐歌曲")
            event.stop_event()
            return
        await service.list_to_session(event, f"好歌精选 · {card or '热门'}", songs)
    except ApiError as err:
        service.log_warn(f"好歌失败: {err}")
        await service.reply(event, f"获取好歌失败：{err}")
    event.stop_event()


async def theme_playlist_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg主题歌单 [序号]：主题歌单列表 / 查看主题曲目"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*主题歌单\s*(.*)$")
    if not m:
        return
    arg = m.group(1).strip()
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope, kind="theme")
    try:
        if re.fullmatch(r"\d+", arg) and session and session.get("type") == "kg_theme":
            n = int(arg)
            themes = session.get("data") or []
            if n < 1 or n > len(themes):
                await service.reply(event, INDEX_OUT_OF_RANGE.format(len(themes)))
                event.stop_event()
                return
            theme = themes[n - 1]
            songs = await kgapi.theme_playlist_tracks(theme.get("id"), pagesize=30)
            if not songs:
                await service.reply(event, f"主题「{theme.get('name') or ''}」暂无曲目")
                event.stop_event()
                return
            await service.list_to_session(event, f"主题 · {theme.get('name') or ''}", songs)
            event.stop_event()
            return
        themes = await kgapi.theme_playlists(pagesize=20)
        if not themes:
            await service.reply(event, "暂无主题歌单")
            event.stop_event()
            return
        await cardlib.SessionStore.set(service.plugin, scope, {"type": "kg_theme", "data": themes}, kind="theme")
        rows = [{"name": t.get("name") or "", "sub": t.get("intro") or "", "cover": t.get("cover") or ""} for t in themes]
        data, format_text = _generic_panel(
            "主题歌单",
            rows,
            subtitle=f"共 {len(themes)} 个主题",
            tip=TIP_THEME_PLAYLIST,
            cfg=service.cfg(),
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"主题歌单失败: {err}")
        await service.reply(event, f"获取主题歌单失败：{err}")
    event.stop_event()


async def yueku_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg乐库：乐库各区块概览"""
    if not service.cfg().get("enable", True):
        return
    try:
        info = await kgapi.yueku() or {}
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
            await service.reply(event, "暂无乐库数据")
            event.stop_event()
            return
        data, format_text = _generic_panel(
            "酷狗乐库",
            items,
            subtitle="乐库各区块",
            tip=TIP_YUEKU,
            cfg=service.cfg(),
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"乐库失败: {err}")
        await service.reply(event, f"获取乐库失败：{err}")
    event.stop_event()


async def top_ip_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg编辑精选：编辑精选专题"""
    if not service.cfg().get("enable", True):
        return
    try:
        items = await kgapi.top_ip(pagesize=15)
        if not items:
            await service.reply(event, "暂无编辑精选数据")
            event.stop_event()
            return
        rows = [{"name": it.get("name") or "", "sub": it.get("sub") or "", "cover": it.get("cover") or ""} for it in items]
        # 文本兜底只展示名称，按同一份 items 投影一遍
        text_rows = [{"name": it.get("name") or ""} for it in items]
        data, format_text = _generic_panel(
            "编辑精选",
            rows,
            subtitle="编辑精选专题",
            cfg=service.cfg(),
            text_rows=text_rows,
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"编辑精选失败: {err}")
        await service.reply(event, f"获取编辑精选失败：{err}")
    event.stop_event()


async def rank_top_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg排行推荐：推荐的排行榜"""
    if not service.cfg().get("enable", True):
        return
    try:
        items = await kgapi.rank_top(pagesize=15)
        if not items:
            await service.reply(event, "暂无排行推荐数据")
            event.stop_event()
            return
        rows = [{"name": it.get("name") or "", "cover": it.get("cover") or ""} for it in items]
        data, format_text = _generic_panel(
            "推荐排行榜",
            rows,
            subtitle="精选榜单",
            tip=TIP_RANK_BY_NAME,
            cfg=service.cfg(),
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"排行推荐失败: {err}")
        await service.reply(event, f"获取排行推荐失败：{err}")
    event.stop_event()


async def history_daily(service: MusicService, event: AstrMessageEvent):
    """#kg历史日推 [序号]：历史每日推荐 / 查看某天歌曲"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*历史日推\s*(.*)$")
    if not m:
        return
    arg = m.group(1).strip()
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope, kind="history")
    try:
        if re.fullmatch(r"\d+", arg) and session and session.get("type") == "kg_history":
            n = int(arg)
            groups = session.get("data") or []
            if n < 1 or n > len(groups):
                await service.reply(event, INDEX_OUT_OF_RANGE.format(len(groups)))
                event.stop_event()
                return
            group = groups[n - 1]
            await service.list_to_session(event, f"历史日推 · {group.get('name') or ''}", group.get("songs") or [])
            event.stop_event()
            return
        groups = await kgapi.everyday_history()
        if not groups:
            await service.reply(event, "暂无历史推荐记录")
            event.stop_event()
            return
        await cardlib.SessionStore.set(service.plugin, scope, {"type": "kg_history", "data": groups}, kind="history")
        rows = [{"name": g.get("name") or "", "tag": f"{g.get('count')} 首"} for g in groups]
        data, format_text = _generic_panel(
            "历史每日推荐",
            rows,
            subtitle=f"共 {len(groups)} 期",
            tip=TIP_HISTORY,
            cfg=service.cfg(),
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"历史日推失败: {err}")
        await service.reply(event, f"获取历史日推失败：{err}")
    event.stop_event()


async def artist_albums_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg歌手专辑 歌手：歌手的专辑列表"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*歌手专辑\s+(.+)$")
    if not m:
        return
    kw = m.group(1).strip()
    try:
        a = await service.resolve_artist(kw)
        if not a:
            await service.reply(event, NOT_FOUND_SINGER.format(kw))
            event.stop_event()
            return
        albums = await kgapi.artist_albums(a.get("id"), pagesize=15)
        if not albums:
            await service.reply(event, f"歌手「{a.get('name') or ''}」暂无专辑")
            event.stop_event()
            return
        rows = [
            {"name": al.get("name") or "", "sub": al.get("publishDate") or "", "cover": al.get("cover") or ""}
            for al in albums
        ]
        data, format_text = _generic_panel(
            f"专辑 · {a.get('name') or ''}",
            rows,
            subtitle=f"共 {len(albums)} 张专辑",
            tip=TIP_ALBUM_TRACKS,
            cfg=service.cfg(),
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"歌手专辑失败: {err}")
        await service.reply(event, f"获取歌手专辑失败：{err}")
    event.stop_event()


async def artist_list_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg歌手列表 [华语/欧美/日韩/日本/韩国]：歌手列表"""
    m = service.check_cmd(event, r"^#?(?:kg|KG)\s*歌手列表\s*(.*)$")
    if not m:
        return
    t = m.group(1).strip()
    type_ = ARTIST_LIST_TYPES.get(t, 0)
    try:
        artists = await kgapi.artist_lists(type_, hotsize=15)
        if not artists:
            await service.reply(event, "暂无歌手数据")
            event.stop_event()
            return
        rows = [{"name": a.get("name") or "", "cover": a.get("cover") or ""} for a in artists]
        data, format_text = _generic_panel(
            f"歌手列表 · {t or '全部'}",
            rows,
            subtitle=f"共 {len(artists)} 位歌手",
            tip=TIP_SINGER_HOT,
            cfg=service.cfg(),
        )
        await service.reply_card_or_text(
            event, tpl_name="kg-generic", data=data, format_text=format_text
        )
    except ApiError as err:
        service.log_warn(f"歌手列表失败: {err}")
        await service.reply(event, f"获取歌手列表失败：{err}")
    event.stop_event()


async def recommend(service: MusicService, event: AstrMessageEvent):
    """#kg推荐 / #kg日推：每日推荐歌曲"""
    if not service.cfg().get("enable", True):
        return
    try:
        songs = await kgapi.everyday_recommend()
        if not songs:
            await service.reply(event, "今日暂无推荐")
            event.stop_event()
            return
        await service.list_to_session(event, "每日推荐", songs[:20])
    except ApiError as err:
        service.log_warn(f"日推失败: {err}")
        await service.reply(event, f"获取每日推荐失败：{err}\n可能需要 #kg登录")
    event.stop_event()


async def random_song(service: MusicService, event: AstrMessageEvent):
    """#kg来首歌：随机来一首（私人 FM 兜底每日推荐）"""
    if not service.cfg().get("enable", True):
        return
    try:
        try:
            songs = await kgapi.personal_fm()
        except ApiError as e:
            # 总超时（20 秒没回）不退回日推再等一轮：两档都是完整 API 调用，
            # 逐档重试只会把一次超时放大成两次
            if e.timeout:
                raise
            songs = await kgapi.everyday_recommend()
        if not songs:
            await service.reply(event, "没有拿到推荐歌曲，请稍后再试")
            event.stop_event()
            return
        import random
        song = random.choice(songs)
        await service.play_song(event, song, source="推荐")
    except ApiError as err:
        service.log_warn(f"来首歌失败: {err}")
        await service.reply(event, f"随机点歌失败：{err}")
    event.stop_event()


async def fm(service: MusicService, event: AstrMessageEvent):
    """#kgFM：私人 FM 歌曲列表"""
    if not service.cfg().get("enable", True):
        return
    try:
        songs = await kgapi.personal_fm()
        if not songs:
            await service.reply(event, "暂无 FM 歌曲")
            event.stop_event()
            return
        await service.list_to_session(event, "私人 FM", songs[:15])
    except ApiError as err:
        service.log_warn(f"FM 失败: {err}")
        await service.reply(event, f"获取 FM 失败：{err}\n可能需要 #kg登录")
    event.stop_event()


def _generic_panel(title, rows, *, subtitle: str = "", tip: str = "", cfg: dict | None = None, text_rows: list | None = None):
    """构造 (kg-generic 卡片数据, 文本兜底回调)，供 explore 各指令统一使用。

    原先每个指令把「卡片 items 列表推导」与「文本兜底 lambda 里的同一份推导」各写
    一遍（10+ 处）；这里只构造一次 rows，卡片与文本兜底共用同一份数据。

    ``format_generic_text`` 只读 name/main/sub/tag，因此除个别「文本只展示部分字段」
    的指令（用 ``text_rows`` 显式投影）外，文本兜底可直接消费 ``rows``。
    """
    data = cardlib.build_generic_card_data(title, rows, subtitle=subtitle, tip=tip, cfg=cfg)
    fallback_rows = rows if text_rows is None else text_rows

    def _format_text(_data):
        return cardlib.format_generic_text(title, fallback_rows, tip=tip)

    return data, _format_text


ROUTES = [
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*排行\s*(.*)$", re.IGNORECASE),
        name="chart",
        doc="#kg排行 [榜单名]：查看排行榜列表或具体榜单歌曲",
        run=chart,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*歌手\s+(.+)$", re.IGNORECASE),
        name="artist",
        doc="#kg歌手 关键词：查看歌手热门歌曲",
        run=artist,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*专辑\s+(.+)$", re.IGNORECASE),
        name="album",
        doc="#kg专辑 关键词：查看专辑曲目",
        run=album,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*歌单\s+(.+)$", re.IGNORECASE),
        name="playlist",
        doc="#kg歌单 关键词|id：搜索歌单并查看曲目（VIP 歌单需登录）",
        run=playlist,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*新歌\s*$", re.IGNORECASE),
        name="new_song",
        doc="#kg新歌：新歌速递",
        run=new_song,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*精品歌单$", re.IGNORECASE),
        name="top_playlist",
        doc="#kg精品歌单：精选歌单",
        run=top_playlist,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*歌单分类$", re.IGNORECASE),
        name="catlist",
        doc="#kg歌单分类：歌单分类列表",
        run=catlist,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*搜索建议\s+(.+)$", re.IGNORECASE),
        name="suggest",
        doc="#kg搜索建议 关键词：关键词补全",
        run=suggest,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*新碟\s*(.*)$", re.IGNORECASE),
        name="new_album",
        doc="#kg新碟 [华语/欧美/日本/韩国]：新碟上架",
        run=new_album,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*好歌\s*(.*)$", re.IGNORECASE),
        name="good_song",
        doc="#kg好歌 [精选/怀旧/热门/小众/VIP]：好歌精选卡片",
        run=good_song,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*主题歌单\s*(.*)$", re.IGNORECASE),
        name="theme_playlist_cmd",
        doc="#kg主题歌单 [序号]：主题歌单列表 / 查看主题曲目",
        run=theme_playlist_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*乐库$", re.IGNORECASE),
        name="yueku_cmd",
        doc="#kg乐库：乐库各区块概览",
        run=yueku_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*编辑精选$", re.IGNORECASE),
        name="top_ip_cmd",
        doc="#kg编辑精选：编辑精选专题",
        run=top_ip_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*排行推荐$", re.IGNORECASE),
        name="rank_top_cmd",
        doc="#kg排行推荐：推荐的排行榜",
        run=rank_top_cmd,
        # 必须高于 #kg排行（^…排行\s*(.*)$ 会把「推荐」吃成榜名并 stop_event）
        priority=1,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*历史日推\s*(.*)$", re.IGNORECASE),
        name="history_daily",
        doc="#kg历史日推 [序号]：历史每日推荐 / 查看某天歌曲",
        run=history_daily,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*歌手专辑\s+(.+)$", re.IGNORECASE),
        name="artist_albums_cmd",
        doc="#kg歌手专辑 歌手：歌手的专辑列表",
        run=artist_albums_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*歌手列表\s*(.*)$", re.IGNORECASE),
        name="artist_list_cmd",
        doc="#kg歌手列表 [华语/欧美/日韩/日本/韩国]：歌手列表",
        run=artist_list_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*(推荐|日推|每日推荐)$", re.IGNORECASE),
        name="recommend",
        doc="#kg推荐 / #kg日推：每日推荐歌曲",
        run=recommend,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*(来首歌|随机|放一首|来一首)$", re.IGNORECASE),
        name="random_song",
        doc="#kg来首歌：随机来一首（私人 FM 兜底每日推荐）",
        run=random_song,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*FM$", re.IGNORECASE),
        name="fm",
        doc="#kgFM：私人 FM 歌曲列表",
        run=fm,
    ),
]
