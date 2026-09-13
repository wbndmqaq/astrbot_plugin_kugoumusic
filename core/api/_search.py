"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import request
from ._normalize import (
    _cover,
    _data_of,
    _duration_text,
    _normalize_album,
    _normalize_all,
    _normalize_artist,
    _normalize_playlist,
    _normalize_song,
    _singers,
)

# ──────────── 搜索 ────────────


async def search(keywords: str, type_: str = "song", page: int = 1, pagesize: int = 10) -> list:
    body = await request(
        "/search", {"keywords": keywords, "type": type_, "page": page, "pagesize": pagesize}, anon=True
    )
    items = _data_of(body).get("lists") or []
    out = []
    if type_ == "song":
        out = _normalize_all(items, _normalize_song)
    elif type_ == "special":
        out = _normalize_all(items, _normalize_playlist)
    elif type_ == "album":
        out = _normalize_all(items, _normalize_album)
    elif type_ == "author":
        out = _normalize_all(items, _normalize_artist)
    elif type_ == "mv":
        for i, m in enumerate(items):
            if not isinstance(m, dict):
                continue
            out.append(
                {
                    "index": i + 1,
                    "id": m.get("mvhash") or m.get("MvHash") or m.get("FileHash") or "",
                    "name": m.get("MvName") or m.get("mvname") or m.get("SongName") or "",
                    "artist": _singers(m) or "",
                    "cover": _cover(m) or "",
                    "duration": _duration_text(m.get("Duration") or m.get("duration")),
                }
            )
    return out
async def hot_search() -> list:
    body = await request("/search/hot", anon=True)
    # 实测结构：data.list[0].keywords[] 是热搜词列表；data.list 本身是分栏
    lists = _data_of(body).get("list") or []
    out = []
    for sec in lists:
        if not isinstance(sec, dict):
            continue
        for k in sec.get("keywords") or []:
            if not isinstance(k, dict):
                continue
            w = k.get("keyword") or k.get("reason") or ""
            if w:
                out.append({"word": str(w), "reason": str(k.get("reason") or "")})
    return out[:15]
async def search_suggest(keywords: str) -> list:
    body = await request("/search/suggest", {"keywords": keywords}, anon=True)
    # 实测结构：data 是数组，每项含 RecordDatas[]，取 HintInfo
    data = (body or {}).get("data") or []
    out = []
    for sec in data:
        if not isinstance(sec, dict):
            continue
        for rd in sec.get("RecordDatas") or []:
            if isinstance(rd, dict) and rd.get("HintInfo"):
                w = str(rd["HintInfo"]).strip()
                if w and w not in out:
                    out.append(w)
            if len(out) >= 10:
                break
        if len(out) >= 10:
            break
    return out[:10]
