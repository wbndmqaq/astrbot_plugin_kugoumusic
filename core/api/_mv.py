"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import request

# ──────────── MV ────────────


async def video_url(hash_: str) -> str:
    body = await request("/video/url", {"hash": hash_})
    data = (body or {}).get("data")
    if not isinstance(data, dict):
        return ""
    # 上游返回的 key 大小写不固定，统一按请求 hash 原样/小写/大写尝试
    for key in (hash_, str(hash_).lower(), str(hash_).upper()):
        item = data.get(key)
        if isinstance(item, dict) and item.get("downurl"):
            return str(item["downurl"])
    return ""
