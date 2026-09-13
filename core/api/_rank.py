"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import request
from ._normalize import _data_of, _normalize_all, _normalize_song

# ──────────── 排行 ────────────


async def rank_list() -> list:
    # 上游 rank_list.js 是 `withsong: params.withsong || 1`——0 是假值会被改成 1，
    # 所以这里显式传 1，与真实行为一致（榜单本就带歌曲列表）。
    body = await request("/rank/list", {"withsong": 1})
    info = _data_of(body).get("info") or []
    out = []
    for i, r in enumerate(info):
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": r.get("rankid") or 0,
                "name": r.get("rankname") or "",
                "cover": str(r.get("album_img_9") or r.get("banner_9") or "").replace("{size}", "300"),
                "update": r.get("new_cycle") or "",
            }
        )
    return out
async def rank_audio(rankid, page: int = 1, pagesize: int = 20) -> list:
    body = await request("/rank/audio", {"rankid": rankid, "page": page, "pagesize": pagesize})
    songs = _data_of(body).get("songlist") or []
    return _normalize_all(songs, _normalize_song)
async def top_song(rank_id: int = 21608, page: int = 1, pagesize: int = 20) -> list:
    body = await request("/top/song", {"type": rank_id, "page": page, "pagesize": pagesize})
    songs = (body or {}).get("data") or []
    return _normalize_all(songs, _normalize_song)
async def related_songs(mixsongid: str, limit: int = 10) -> list:
    body = await request("/audio/related", {"album_audio_id": mixsongid})
    items = (body or {}).get("data") or []
    return _normalize_all(items[:limit], _normalize_song)
