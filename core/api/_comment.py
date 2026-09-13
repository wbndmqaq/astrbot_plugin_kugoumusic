"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import _num, request

# ──────────── 评论 ────────────


async def comment_music(mixsongid: str, page: int = 1, pagesize: int = 20) -> dict:
    body = await request("/comment/music", {"mixsongid": mixsongid, "page": page, "pagesize": pagesize})
    comments = []
    for i, c in enumerate((body or {}).get("list") or []):
        if not isinstance(c, dict):
            continue
        comments.append(
            {
                "index": i + 1,
                "nick": c.get("user_name") or "",
                "avatar": str(c.get("user_pic") or "").replace("{size}", "165"),
                "time": c.get("addtime") or "",
                "likes": int(_num(c.get("reply_num") or c.get("like_num"))),
                "content": c.get("content") or "",
                "hot": bool(c.get("hot")),
            }
        )
    return {
        "comments": comments,
        "count": int(_num((body or {}).get("count"))),
        "childrenid": (body or {}).get("childrenid") or "",
    }
# ──────────── 评论扩展 ────────────


async def comment_count(hash_: str) -> int:
    body = await request("/comment/count", {"hash": hash_})
    # 实测返回 {hash: count} 字典
    if isinstance(body, dict):
        target = hash_.lower().replace("-", "")
        # 先按本次请求的 hash 精确匹配（大小写/连字符不敏感）
        for k, v in body.items():
            if k.lower().replace("-", "") == target:
                return int(_num(v))
        # 退回「唯一的 32 位 key」：原先只判 len(k)==32，字典里若有多个 32 位键会取到
        # 第一个，可能不是本次请求的 hash（评论数张冠李戴）。
        hashed = [(k, v) for k, v in body.items() if len(k) == 32]
        if len(hashed) == 1:
            return int(_num(hashed[0][1]))
    return 0
async def comment_playlist(playlist_id, page: int = 1, pagesize: int = 20) -> list:
    body = await request("/comment/playlist", {"id": playlist_id, "page": page, "pagesize": pagesize})
    return _parse_comments((body or {}).get("list") or [])
async def comment_album(album_id, page: int = 1, pagesize: int = 20) -> list:
    body = await request("/comment/album", {"id": album_id, "page": page, "pagesize": pagesize})
    return _parse_comments((body or {}).get("list") or [])
def _parse_comments(items: list) -> list:
    out = []
    for i, c in enumerate(items):
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "nick": c.get("user_name") or c.get("nickname") or "",
                "time": str(c.get("addtime") or "")[:10],
                "likes": int(_num(c.get("reply_num") or c.get("like_num") or c.get("liked_count"))),
                "content": c.get("content") or "",
            }
        )
    return out
