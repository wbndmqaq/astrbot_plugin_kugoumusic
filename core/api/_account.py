"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

import time

from ._core import _num, request, set_device_cookie
from ._normalize import _cover, _data_of, _normalize_all, _normalize_playlist, _normalize_song

# ──────────── 设备注册 / 登录 ────────────


async def register_dev() -> str:
    """注册/获取设备 dfid，缓存到 api 模块（main.py 持久化到 plugin_data）。"""
    body = await request("/register/dev", {}, "get", inject_cookie=False)
    dfid = _data_of(body).get("dfid") or ""
    if dfid:
        set_device_cookie(f"dfid={dfid}")
    return dfid
async def qr_key() -> str:
    # KuGouMusicApi 对所有 200 响应缓存 2 分钟，二维码接口必须拼 timestamp 绕过
    body = await request("/login/qr/key", {"timestamp": int(time.time() * 1000)})
    return _data_of(body).get("qrcode") or ""
async def qr_create(key: str) -> dict:
    body = await request("/login/qr/create", {"key": key, "qrimg": "true"})
    data = _data_of(body)
    return {
        "url": data.get("url") or "",
        "base64": data.get("base64") or "",
    }
async def qr_check(key: str) -> dict:
    body = await request("/login/qr/check", {"key": key, "timestamp": int(time.time() * 1000)})
    data = _data_of(body)
    return {
        "status": data.get("status"),
        "token": data.get("token") or "",
        "userid": data.get("userid") or "",
        "nickname": data.get("nickname") or data.get("user_name") or "",
        "body": body,
    }
async def login_qq_qr_create() -> dict:
    """QQ 扫码登录：生成二维码及上下文。"""
    body = await request("/login/qq/qr/create", {"timestamp": int(time.time() * 1000)})
    return body or {}
async def login_qq_qr_check(params: dict) -> dict:
    """QQ 扫码登录：轮询扫码状态并完成登录。"""
    req_params = dict(params or {})
    req_params["timestamp"] = int(time.time() * 1000)
    body = await request("/login/qq/qr/check", req_params)
    data = _data_of(body)
    return {
        "status": (body or {}).get("status") if "status" in (body or {}) else data.get("status"),
        "msg": (body or {}).get("msg") or data.get("msg") or "",
        "token": data.get("token") or (body or {}).get("token") or "",
        "userid": data.get("userid") or (body or {}).get("userid") or "",
        "nickname": data.get("nickname") or data.get("username") or "",
        "data": data,
        "body": body,
    }
# ──────────── 用户（需登录） ────────────


async def user_detail(userid: str) -> dict:
    body = await request("/user/detail", {"userid": userid})
    data = (body or {}).get("data")
    if isinstance(data, dict):
        return {
            "id": data.get("userid") or userid,
            "name": data.get("username") or data.get("nickname") or "",
            "avatar": _cover(data) or str(data.get("user_img") or data.get("avatar") or "").replace("{size}", "300"),
            "vip": bool(data.get("is_vip")),
            "level": int(_num(data.get("level") or data.get("user_level"))),
        }
    return {}
async def user_playlist(userid: str, page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/playlist", {"userid": userid, "page": page, "pagesize": pagesize})
    data = (body or {}).get("data")
    # 实测结构：data.info[]（含 global_collection_id），兼容 list/plist
    pls = data.get("info") if isinstance(data, dict) else []
    if not pls:
        pls = data.get("list") if isinstance(data, dict) else []
    if not pls:
        pls = (body or {}).get("list") or []
    return _normalize_all(pls, _normalize_playlist)
async def user_listen(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/listen", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data")
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def user_history(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/history", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data")
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
# ──────────── 账号扩展（需登录） ────────────


async def user_cloud(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/cloud", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data")
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def user_purchased_songs(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/purchased/songs", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data")
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def user_purchased_albums(page: int = 1, pagesize: int = 20) -> list:
    body = await request("/user/purchased/albums", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data")
    albums = data.get("album_list") if isinstance(data, dict) else []
    if not albums:
        albums = (body or {}).get("album_list") or (body or {}).get("list") or []
    out = []
    for i, a in enumerate(albums):
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": a.get("album_id") or a.get("albumid") or 0,
                "name": a.get("album_name") or a.get("albumname") or "",
                "cover": str(a.get("sizable_cover") or a.get("cover") or "").replace("{size}", "300"),
                "artist": a.get("author_name") or a.get("singername") or "",
            }
        )
    return out
async def user_grade_info() -> dict:
    body = await request("/user/grade/info")
    data = (body or {}).get("data")
    if not isinstance(data, dict):
        return {}
    return {
        "dSec": int(_num(data.get("d_sec"))),
        "grade": int(_num(data.get("p_grade"))),
        "currentPoint": int(_num(data.get("p_current_point"))),
        "nextGrade": int(_num(data.get("p_next_grade"))),
        "nextGradePoint": int(_num(data.get("p_next_grade_point"))),
        "servertime": str(data.get("servertime") or ""),
    }
async def artist_follow(author_id) -> dict:
    return await request("/artist/follow", {"id": author_id})
async def artist_unfollow(author_id) -> dict:
    return await request("/artist/unfollow", {"id": author_id})
async def artist_follow_newsongs(pagesize: int = 30) -> list:
    body = await request("/artist/follow/newsongs", {"pagesize": pagesize})
    data = (body or {}).get("data")
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def user_follow(page: int = 1, pagesize: int = 30) -> list:
    """用户关注歌手列表。"""
    body = await request("/user/follow", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data")
    lst = data.get("list") if isinstance(data, dict) else []
    if not lst:
        lst = (body or {}).get("list") or []
    out = []
    for i, a in enumerate(lst):
        if not isinstance(a, dict):
            continue
        singer = a.get("singer") if isinstance(a.get("singer"), dict) else a
        out.append(
            {
                "index": i + 1,
                "id": singer.get("singerid") or singer.get("author_id") or 0,
                "name": singer.get("singername") or singer.get("author_name") or "",
                "cover": str(singer.get("sizable_avatar") or singer.get("avatar") or "").replace("{size}", "300"),
            }
        )
    return out
# ──────────── 登录维护 ────────────


async def login_token_refresh() -> dict:
    """刷新登录 token，延长过期时间。"""
    return await request("/login/token", {"timestamp": int(time.time() * 1000)})
async def playhistory_upload(mxid: str) -> dict:
    """上报听歌历史（需登录）。

    上游 ``playhistory_upload.js`` 读的是 ``time``（秒级时间戳），``op``/``pc`` 取默认值；
    原实现发的是 ``ot``，服务端收不到该参数（只是恰好有「服务器当前时间」兜底，
    所以没暴露出来）。
    """
    return await request("/playhistory/upload", {"mxid": mxid, "time": int(time.time())})
