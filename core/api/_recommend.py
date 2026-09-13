"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import request
from ._normalize import _data_of, _normalize_all, _normalize_song

# ──────────── 推荐 / FM ────────────


async def everyday_recommend() -> list:
    body = await request("/everyday/recommend")
    songs = _data_of(body).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def personal_fm() -> list:
    body = await request("/personal/fm")
    songs = _data_of(body).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
