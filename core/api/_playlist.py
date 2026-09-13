"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import _num, request
from ._normalize import _data_of, _normalize_all, _normalize_playlist, _normalize_song

# ──────────── 歌手 / 专辑 ────────────


async def artist_audios(author_id, sort: str = "hot", page: int = 1, pagesize: int = 30) -> list:
    body = await request("/artist/audios", {"id": author_id, "sort": sort, "page": page, "pagesize": pagesize})
    songs = (body or {}).get("data") or []
    return _normalize_all(songs, _normalize_song)
async def album_songs(album_id, page: int = 1, pagesize: int = 30) -> list:
    body = await request("/album/songs", {"id": album_id, "page": page, "pagesize": pagesize})
    songs = _data_of(body).get("songs") or []
    return _normalize_all(songs, _normalize_song)
# ──────────── 歌单 ────────────


async def playlist_tracks(playlist_id, page: int = 1, pagesize: int = 100) -> list:
    body = await request("/playlist/track/all", {"id": playlist_id, "page": page, "pagesize": pagesize})
    songs = _data_of(body).get("songs") or []
    return _normalize_all(songs, _normalize_song)
async def top_playlists(category_id: int = 0, page: int = 1, pagesize: int = 15) -> list:
    body = await request("/top/playlist", {"category_id": category_id, "page": page, "pagesize": pagesize})
    pls = _data_of(body).get("special_list") or []
    return _normalize_all(pls, _normalize_playlist)
async def playlist_tags() -> list:
    body = await request("/playlist/tags")
    data = (body or {}).get("data") or []
    out = []
    for i, t in enumerate(data):
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "name": t.get("tag_name") or t.get("name") or "",
                "id": t.get("tag_id") or t.get("id") or 0,
                "count": int(_num(t.get("tag_count") or t.get("count"))),
            }
        )
    return out
# ──────────── 新碟 / 推荐卡片 / 主题 ────────────


async def top_album(area: int = 0, page: int = 1, pagesize: int = 20) -> list:
    """新碟上架。type：1 华语 / 2 欧美 / 3 日本 / 4 韩国 / 0 推荐。"""
    params = {"type": area, "page": page, "pagesize": pagesize}
    # 实测：/top/album 与 /search 一样要求非空 token/userid 占位，否则 20010
    body = await request("/top/album", params, anon=True)
    chn = _data_of(body).get("chn") or []
    out = []
    for i, a in enumerate(chn):
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": a.get("albumid") or a.get("album_id") or 0,
                "name": a.get("albumname") or a.get("album_name") or "",
                "cover": str(a.get("sizable_cover") or a.get("cover") or a.get("img") or "").replace("{size}", "300"),
                "artist": a.get("singername") or a.get("author_name") or "",
                "publishDate": str(a.get("publishtime") or a.get("publish_date") or ""),
            }
        )
    return out
async def top_card(card_id: int = 3, pagesize: int = 20) -> list:
    """歌曲推荐卡片。card_id：1 精选好歌 / 2 经典怀旧 / 3 热门好歌 / 4 小众宝藏 / 6 VIP 专属。"""
    body = await request("/top/card", {"card_id": card_id, "pagesize": pagesize})
    songs = _data_of(body).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def theme_playlists(page: int = 1, pagesize: int = 20) -> list:
    body = await request("/theme/playlist", {"page": page, "pagesize": pagesize})
    lst = _data_of(body).get("theme_list") or []
    out = []
    for i, t in enumerate(lst):
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": t.get("id") or 0,
                "name": t.get("title") or "",
                "cover": str(t.get("pic") or t.get("pic_net_save") or "").replace("{size}", "300"),
                "intro": str(t.get("intro") or "")[:80],
            }
        )
    return out
async def theme_playlist_tracks(theme_id, pagesize: int = 30) -> list:
    body = await request("/theme/playlist/track", {"theme_id": theme_id, "pagesize": pagesize})
    data = (body or {}).get("data")
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def yueku() -> dict:
    """乐库：返回各区块条目数，供 #kg乐库 展示。"""
    body = await request("/yueku")
    info = _data_of(body).get("info") or {}
    if not isinstance(info, dict):
        return {}
    sections = {}
    for key in ("recommend", "song", "rank", "album", "video", "topic"):
        v = info.get(key)
        if isinstance(v, list):
            sections[key] = len(v)
        elif isinstance(v, dict) and v.get("list"):
            sections[key] = len(v["list"])
    return {"sections": sections, "raw": info}
async def top_ip(pagesize: int = 20) -> list:
    """编辑精选。"""
    body = await request("/top/ip", {"pagesize": pagesize})
    lst = _data_of(body).get("list") or []
    out = []
    for i, it in enumerate(lst):
        if not isinstance(it, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": it.get("id") or 0,
                "name": it.get("title") or "",
                "cover": str(it.get("sizable_image_url") or it.get("image_url") or "")
                .replace("{size}x{size}", "400")
                .replace("{size}", "400"),
                "sub": str(it.get("sub_title") or it.get("intro") or "")[:60],
            }
        )
    return out
async def rank_top(pagesize: int = 20) -> list:
    """排行榜推荐列表。"""
    body = await request("/rank/top", {"pagesize": pagesize})
    lst = _data_of(body).get("list") or []
    out = []
    for i, r in enumerate(lst):
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": r.get("rankid") or 0,
                "name": r.get("rankname") or "",
                "cover": str(r.get("banner_9") or r.get("album_img_9") or "").replace("{size}", "300"),
                "sub": str(r.get("intro") or "")[:60],
            }
        )
    return out
async def everyday_history() -> list:
    """历史每日推荐：返回 [{date, name, songs}]。"""
    body = await request("/everyday/history", {"mode": "list"})
    lst = _data_of(body).get("lists") or []
    out = []
    for i, it in enumerate(lst):
        if not isinstance(it, dict):
            continue
        songs = it.get("song_list") or []
        norm_songs = _normalize_all(songs, _normalize_song)
        if not norm_songs:
            continue
        out.append(
            {
                "index": i + 1,
                "name": str(it.get("date") or it.get("name") or f"历史推荐 {i + 1}"),
                "songs": norm_songs,
                "count": len(norm_songs),
            }
        )
    return out
