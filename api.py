from __future__ import annotations

import re
import time
from typing import Any

import aiohttp

# 酷狗 API 错误码（KuGouMusicApi server 把上游失败统一转成 HTTP 502，
# body 携带 error_code；业务层面 status==1 成功、status==2 音质/付费降级信号）
ERR_MESSAGES = {
    152: "搜索参数被拒（152）：酷狗需要有效设备 Cookie，请确认 API 服务正常并已注册设备",
    20010: "需要登录（20010）：请发送 #kg登录 扫码登录后使用",
    20017: "需要登录或 Token 失效（20017）：请发送 #kg登录",
    20018: "需要登录或 Token 失效（20018）：请发送 #kg登录",
    20028: "请求触发风控（20028）：请求过快或设备 Cookie 异常，冷却后重试",
    20040: "设备 Cookie 异常（20040）：请重启插件重新注册设备",
}


class ApiError(Exception):
    def __init__(self, message: str, *, code=None, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload


# 模块级配置访问器，由 main.py 注入
_cfg_getter = None


def set_config_getter(fn):
    global _cfg_getter
    _cfg_getter = fn


def _cfg() -> dict:
    if _cfg_getter is not None:
        try:
            return _cfg_getter() or {}
        except Exception:
            return {}
    return {}


# 登录 Cookie（插件配置 defaultCookie：token;userid;vip_type;vip_token 等）
def _get_cookie() -> str:
    try:
        return str(_cfg().get("defaultCookie") or "")
    except Exception:
        return ""


# 设备 Cookie（dfid，来自 /register/dev，main.py 启动时注入并持久化）
_device_cookie = ""


def set_device_cookie(cookie: str):
    global _device_cookie
    _device_cookie = cookie or ""


def get_device_cookie() -> str:
    return _device_cookie


def _compose_cookie_str(*, anon: bool = False) -> str:
    """组合请求 Cookie：设备 dfid + 登录 Cookie（无登录时可补非空匿名占位）。

    注意：匿名占位 token=kg;userid=1 只对 /search 必需（空值会被 KuGouMusicApi
    服务端 cookie 解析器剥掉 → 152）；对 song/url 等取链接口传伪造 token 反而
    触发 502 "token api error"，因此取链类接口默认不补占位。
    """
    parts = []
    if _device_cookie:
        parts.append(_device_cookie)
    uc = _get_cookie()
    if uc:
        parts.append(uc)
    elif anon:
        parts.append("token=kg;userid=1")
    return ";".join(x for x in parts if x)


# ──────────── 基础请求 ────────────


def _get_base() -> str:
    base = str(_cfg().get("apiBase") or "").strip()
    return base.rstrip("/")


def _query_safe_params(params: dict) -> dict:
    out: dict = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = int(v)
        elif isinstance(v, (str, int, float)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _num(v: Any) -> float:
    if v is None or isinstance(v, (list, dict)):
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return f if f == f and f != float("inf") else 0  # NaN/inf guard


def _err_msg_for(code, status: int = 0) -> str:
    try:
        c = int(code) if code is not None else None
    except (TypeError, ValueError):
        c = None
    if c is not None and c in ERR_MESSAGES:
        return ERR_MESSAGES[c]
    if status >= 500:
        return f"酷狗 API 服务错误（HTTP {status}），请检查 KuGouMusicApi 服务"
    if status >= 400:
        return f"请求失败（HTTP {status}）"
    return "请求失败"


async def request(
    pathname: str,
    params: dict | None = None,
    method: str = "get",
    user_key: str = "",
    *,
    inject_cookie: bool = True,
    anon: bool = False,
) -> dict:
    """请求 KuGouMusicApi，返回业务 body dict（已做状态/错误码校验）。

    anon=True 时未登录会补匿名占位 token/userid（仅 /search 类需要）。
    """
    del user_key  # 酷狗为共享账号（配置 defaultCookie），暂无 per-user 分离
    params = dict(params or {})
    base = _get_base()
    if not base:
        raise ApiError("API 地址未配置：请发送 #kg api <地址>，或在插件设置面板填写 apiBase")
    if "://" not in base:
        raise ApiError(f"API 地址格式错误（缺少 http:// 协议头）：{base}")
    url = f"{base}{pathname if pathname.startswith('/') else '/' + pathname}"
    if inject_cookie:
        params["cookie"] = _compose_cookie_str(anon=anon)

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if method == "get":
                async with sess.get(url, params=_query_safe_params(params)) as res:
                    return await _handle_response(res, pathname)
            else:
                # POST 体同样带上 cookie（服务端 query/body 的 cookie 都会被解析合并）
                async with sess.post(url, data=_query_safe_params(params)) as res:
                    return await _handle_response(res, pathname)
    except aiohttp.ClientConnectorError as e:
        raise ApiError(f"无法连接酷狗 API（{base}），请确认 KuGouMusicApi 服务已启动") from e
    except aiohttp.ServerTimeoutError as e:
        raise ApiError(f"请求超时：{base}") from e
    except aiohttp.ClientError as e:
        raise ApiError(f"网络错误：{e}") from e


async def _handle_response(res: aiohttp.ClientResponse, pathname: str = "") -> dict:
    status = res.status
    try:
        data = await res.json(content_type=None)
    except Exception:
        text = (await res.text())[:200]
        raise ApiError(f"返回非 JSON（HTTP {status}）：{text}")
    if not isinstance(data, dict):
        raise ApiError(f"返回格式异常（HTTP {status}）")
    if status >= 400:
        code = data.get("error_code") or data.get("errcode") or data.get("err_code")
        msg = data.get("error_msg") or data.get("errmsg") or data.get("message") or data.get("info") or ""
        raise ApiError(str(msg) or _err_msg_for(code, status), code=code, payload=data)
    # 业务失败：status==0（服务端会转成 502，此处双保险）
    if data.get("status") == 0:
        code = data.get("error_code") or data.get("errcode") or data.get("err_code")
        msg = data.get("error_msg") or data.get("errmsg") or data.get("message") or ""
        raise ApiError(str(msg) or _err_msg_for(code), code=code, payload=data)
    return data


# ──────────── 归一化 ────────────


def _singers(item: dict) -> str:
    """歌手名：兼容 SingerName / author_name / Singers[] / singerinfo[] / authors[]。"""
    for key in ("Singers", "singerinfo", "authors", "singers"):
        arr = item.get(key)
        if isinstance(arr, list):
            names = [a.get("name") or a.get("author_name") for a in arr if isinstance(a, dict)]
            names = [str(n).strip() for n in names if n]
            if names:
                return " / ".join(names)
    for key in ("SingerName", "author_name", "singername", "singer"):
        v = item.get(key)
        if v:
            return str(v)
    return ""


def _cover(item: dict) -> str:
    """封面：优先现成封面（trans_param.union_cover），其余替换 {size} 占位。"""
    tp = item.get("trans_param") if isinstance(item.get("trans_param"), dict) else {}
    candidates = [
        tp.get("union_cover"),
        item.get("Image"),
        item.get("AlbumImage"),
        item.get("sizable_cover"),
        item.get("img"),
        item.get("cover"),
    ]
    for c in candidates:
        if c:
            return str(c).replace("{size}", "300")
    return ""


def _duration_text(ms_or_sec) -> str:
    if ms_or_sec is None:
        return ""
    try:
        f = float(ms_or_sec)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    # 酷狗时长有 秒(Duration/TimeLength) 和 毫秒(timelength/time_length) 两种单位
    if f < 10000:  # 超过 2.7 小时的歌罕见，>10000 一律当毫秒
        sec = int(f)
    else:
        sec = int(f / 1000)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _mix_id(item: dict) -> str:
    for key in ("MixSongID", "mixsongid", "album_audio_id", "Audioid", "audio_id", "songid"):
        v = item.get(key)
        if v is not None and str(v) not in ("", "0"):
            return str(v)
    return ""


def _is_paid(item: dict) -> bool:
    """是否付费/VIP（展示用）：PayType / pay_type / fail_process。"""
    pay = item.get("PayType")
    if pay is None:
        pay = item.get("pay_type")
    if pay is not None:
        try:
            return int(pay) > 0
        except (TypeError, ValueError):
            pass
    fp = item.get("fail_process")
    if fp is not None:
        try:
            return int(fp) != 0
        except (TypeError, ValueError):
            return bool(fp)
    return False


def _base_of(item: dict) -> dict:
    b = item.get("base")
    return b if isinstance(b, dict) else {}


def _audio_info_of(item: dict) -> dict:
    a = item.get("audio_info")
    return a if isinstance(a, dict) else {}


def _album_info_of(item: dict) -> dict:
    a = item.get("album_info")
    return a if isinstance(a, dict) else {}


def _normalize_song(item: dict, idx: int = 0) -> dict | None:
    """把不同来源（搜索/audio/FM/排行/歌单/专辑/相关）的歌曲对象归一化成统一结构。"""
    if not isinstance(item, dict):
        return None
    base = _base_of(item)
    ainfo = _audio_info_of(item)
    alinfo = _album_info_of(item)
    name = (
        base.get("songname")
        or base.get("audio_name")
        or item.get("OriSongName")
        or item.get("official_songname")
        or item.get("songname")
        or item.get("SongName")
        or item.get("audio_name")
        or item.get("name")
        or ""
    )
    name = str(name).strip()
    if not name:
        # 搜索结果的 FileName 形如 "周杰伦 - 晴天"，可拆分兜底
        fn = str(item.get("FileName") or "").strip()
        if fn and " - " in fn:
            _, _, nm = fn.partition(" - ")
            name = nm
    if not name:
        return None
    h128 = str(ainfo.get("hash") or item.get("hash_128") or item.get("hash") or item.get("FileHash") or "")
    album = base.get("album_name") or item.get("AlbumName") or item.get("album_name") or ""
    timelength = ainfo.get("timelength") or item.get("timelength") or item.get("time_length") or 0
    artist = _singers(base) or _singers(item)
    if not artist:
        # 兜底：FileName 形如 "周杰伦 - 晴天"，拆分出歌手
        fn = str(item.get("FileName") or "").strip()
        if fn and " - " in fn:
            artist, _, _ = fn.partition(" - ")
    return {
        "index": idx + 1,
        "id": str(
            base.get("audio_id")
            or item.get("audio_id")
            or item.get("Audioid")
            or item.get("songid")
            or item.get("Scid")
            or 0
        ),
        "mixsongid": _mix_id(base) or _mix_id(item),
        "hash": h128,
        "hash_128": h128,
        "hash_320": str(ainfo.get("hash_320") or item.get("hash_320") or (item.get("HQ") or {}).get("Hash") or ""),
        "hash_flac": str(ainfo.get("hash_flac") or item.get("hash_flac") or (item.get("SQ") or {}).get("Hash") or ""),
        "hash_high": str(
            ainfo.get("hash_high")
            or item.get("hash_high")
            or (item.get("Res") or {}).get("Hash")
            or item.get("hash_flac")
        ),
        "hash_super": str(ainfo.get("hash_super") or item.get("hash_super") or ""),
        "name": name,
        "artist": artist,
        "album": str(album),
        "cover": _cover(item) or str(alinfo.get("cover") or "").replace("{size}", "300"),
        "duration": _duration_text(timelength or item.get("Duration") or item.get("duration") or 0),
        "dtMs": int(_num(timelength)),
        "paid": _is_paid(item),
        "payType": int(_num(item.get("PayType") or item.get("pay_type"))),
        "failProcess": item.get("fail_process") or ainfo.get("fail_process"),
    }


def _normalize_playlist(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("specialname") or item.get("name") or item.get("special_name") or ""
    if not name:
        return None
    return {
        "index": idx + 1,
        "id": item.get("global_collection_id") or item.get("specialid") or item.get("id") or item.get("listid") or 0,
        "name": str(name),
        "cover": _cover(item) or str(item.get("pic") or item.get("img") or "").replace("{size}", "300"),
        "songCount": int(
            _num(item.get("count") or item.get("m_count") or item.get("song_count") or item.get("songcount"))
        ),
        "playCount": int(_num(item.get("play_count") or item.get("playnum") or item.get("listen_num"))),
        "creator": str(
            item.get("list_create_username")
            or item.get("nickname")
            or item.get("author_name")
            or item.get("user_name")
            or ""
        ),
    }


def _normalize_album(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("albumname") or item.get("album_name") or item.get("name") or ""
    if not name:
        return None
    return {
        "index": idx + 1,
        "id": item.get("albumid") or item.get("album_id") or item.get("id") or 0,
        "name": str(name),
        "cover": _cover(item) or str(item.get("img") or "").replace("{size}", "300"),
        "artist": _singers(item) or str(item.get("singer") or ""),
        "songCount": int(_num(item.get("songcount") or item.get("song_count"))),
    }


def _normalize_artist(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("AuthorName") or item.get("author_name") or item.get("name") or ""
    if not name:
        return None
    return {
        "index": idx + 1,
        "id": item.get("AuthorId") or item.get("author_id") or item.get("id") or 0,
        "name": str(name),
        "cover": _cover(item) or str(item.get("Avatar") or item.get("avatar") or "").replace("{size}", "300"),
        "fans": int(_num(item.get("FansNum") or item.get("fans_num"))),
    }


# ──────────── 搜索 ────────────


async def search(keywords: str, type_: str = "song", page: int = 1, pagesize: int = 10) -> list:
    body = await request(
        "/search", {"keywords": keywords, "type": type_, "page": page, "pagesize": pagesize}, anon=True
    )
    items = ((body or {}).get("data") or {}).get("lists") or []
    out = []
    if type_ == "song":
        for i, s in enumerate(items):
            norm = _normalize_song(s, i)
            if norm:
                out.append(norm)
    elif type_ == "special":
        for i, p in enumerate(items):
            norm = _normalize_playlist(p, i)
            if norm:
                out.append(norm)
    elif type_ == "album":
        for i, a in enumerate(items):
            norm = _normalize_album(a, i)
            if norm:
                out.append(norm)
    elif type_ == "author":
        for i, a in enumerate(items):
            norm = _normalize_artist(a, i)
            if norm:
                out.append(norm)
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
    lists = ((body or {}).get("data") or {}).get("list") or []
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


# ──────────── 歌曲详情 / 取链 ────────────


async def audio_by_hash(hash_: str) -> dict | None:
    body = await request("/audio", {"hash": hash_})
    data = (body or {}).get("data") or []
    item = data[0] if data and isinstance(data[0], dict) else {}
    if not item or not (item.get("hash") or item.get("audio_id")):
        return None
    norm = _normalize_song(item)
    if norm:
        norm["id"] = str(item.get("audio_id") or item.get("Audioid") or norm.get("id") or 0)
    return norm


def _pick_url(item: dict) -> str:
    """song/url 返回 url/backupUrl 数组，取第一个可用。"""
    for key in ("url", "backupUrl"):
        v = item.get(key)
        if isinstance(v, list):
            for u in v:
                if u:
                    return str(u)
        elif v:
            return str(v)
    return ""


def _is_trial_url(url: str) -> bool:
    """酷狗 CDN 试听流 URL 含 /yp/p_0_<字节数>/ 标记；全曲为 /yp/full/。"""
    u = (url or "").lower()
    return "p_0_" in u and "full" not in u


async def song_url(hash_: str, quality: str, *, free_part: bool = False) -> dict:
    body = await request(
        "/song/url",
        {"hash": hash_, "quality": quality, "free_part": 1 if free_part else 0},
    )
    return {
        "url": _pick_url(body or {}),
        "status": (body or {}).get("status"),
        "error_code": (body or {}).get("error_code"),
        "fail_process": (body or {}).get("fail_process"),
        "extName": (body or {}).get("extName"),
        "raw": body,
    }


async def song_url_best(song: dict, preferred: str = "auto", *, trial_fallback: bool = True) -> dict:
    """按音质阶梯取链，返回 {url, quality, trial, ...}。

    策略：从偏好音质起向下用「该音质专属 hash」请求 song/url；
    付费歌(status==2)或个别音质 502 都视为该档不可用继续降级；
    全部不可用时若开启 trial_fallback 退到 60s 试听(free_part=1)。
    已登录时（配置 defaultCookie）付费歌也能拿到全曲。
    """
    from .quality import hash_for_quality, quality_candidates

    ladder = quality_candidates(preferred)
    last_err: ApiError | None = None
    last_paid = False
    for q in ladder:
        h = hash_for_quality(song, q)
        if not h:
            continue
        try:
            r = await song_url(h, q)
        except ApiError as e:
            last_err = e  # 付费高音质 502 属正常，继续降级
            continue
        if r.get("url") and r.get("status") == 1:
            # 付费歌即使请求 flac，CDN 也只会给 60s 试听流（URL 含 p_0_ 标记）
            trial = _is_trial_url(r["url"])
            return {
                "url": r["url"],
                "quality": "128" if trial else q,
                "trial": trial,
                "paid": bool(trial),
                "extName": r.get("extName") or "",
                "raw": r["raw"],
            }
        if r.get("status") == 2:
            last_paid = True
    # 全部付费/不可用 → 试听降级
    if trial_fallback:
        h128 = hash_for_quality(song, "128")
        if h128:
            try:
                r = await song_url(h128, "128", free_part=True)
            except ApiError as e:
                last_err = e
            else:
                if r.get("url") and r.get("status") == 1:
                    return {
                        "url": r["url"],
                        "quality": "128",
                        "trial": True,
                        "paid": True,
                        "extName": r.get("extName") or "",
                        "raw": r.get("raw"),
                    }
    if last_err is not None and last_err.code in (20010, 20017, 20028, 20040):
        raise last_err
    if last_paid:
        raise ApiError("该歌曲为付费/VIP，未登录无法获取完整播放链接，请发送 #kg登录", code=20017)
    if last_err is not None:
        raise last_err
    raise ApiError("无法获取播放链接（可能已下架或无版权）")


# ──────────── 歌词 ────────────


async def lyric_search(*, hash_: str = "", keywords: str = "", album_audio_id: str = "") -> list:
    body = await request("/search/lyric", {"hash": hash_, "keywords": keywords, "album_audio_id": album_audio_id})
    candidates = (body or {}).get("candidates") or []
    out = []
    for i, c in enumerate(candidates):
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": c.get("id") or c.get("download_id") or "",
                "accesskey": c.get("accesskey") or "",
                "song": c.get("song") or "",
                "singer": c.get("singer") or "",
                "duration": _duration_text(c.get("duration")),
                "contenttype": c.get("contenttype"),
            }
        )
    return out


async def lyric(id_: str, accesskey: str, fmt: str = "lrc") -> dict:
    body = await request("/lyric", {"id": id_, "accesskey": accesskey, "fmt": fmt, "decode": 1})
    return {
        "content": body.get("content") or "",
        "decodeContent": body.get("decodeContent") or "",
        "fmt": body.get("fmt") or "",
    }


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


# ──────────── 排行 ────────────


async def rank_list() -> list:
    body = await request("/rank/list", {"withsong": 0})
    info = ((body or {}).get("data") or {}).get("info") or []
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
    songs = ((body or {}).get("data") or {}).get("songlist") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def top_song(rank_id: int = 21608, page: int = 1, pagesize: int = 20) -> list:
    body = await request("/top/song", {"type": rank_id, "page": page, "pagesize": pagesize})
    songs = (body or {}).get("data") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def related_songs(mixsongid: str, limit: int = 10) -> list:
    body = await request("/audio/related", {"album_audio_id": mixsongid})
    items = (body or {}).get("data") or []
    out = []
    for i, s in enumerate(items[:limit]):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


# ──────────── 歌手 / 专辑 ────────────


async def artist_audios(author_id, sort: str = "hot", page: int = 1, pagesize: int = 30) -> list:
    body = await request("/artist/audios", {"id": author_id, "sort": sort, "page": page, "pagesize": pagesize})
    songs = (body or {}).get("data") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def album_songs(album_id, page: int = 1, pagesize: int = 30) -> list:
    body = await request("/album/songs", {"id": album_id, "page": page, "pagesize": pagesize})
    songs = ((body or {}).get("data") or {}).get("songs") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


# ──────────── 歌单 ────────────


async def playlist_tracks(playlist_id, page: int = 1, pagesize: int = 100) -> list:
    body = await request("/playlist/track/all", {"id": playlist_id, "page": page, "pagesize": pagesize})
    songs = ((body or {}).get("data") or {}).get("songs") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def top_playlists(category_id: int = 0, page: int = 1, pagesize: int = 15) -> list:
    body = await request("/top/playlist", {"category_id": category_id, "page": page, "pagesize": pagesize})
    pls = ((body or {}).get("data") or {}).get("special_list") or []
    out = []
    for i, p in enumerate(pls):
        norm = _normalize_playlist(p, i)
        if norm:
            out.append(norm)
    return out


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


# ──────────── 推荐 / FM ────────────


async def everyday_recommend() -> list:
    body = await request("/everyday/recommend")
    songs = ((body or {}).get("data") or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def personal_fm() -> list:
    body = await request("/personal/fm")
    songs = ((body or {}).get("data") or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


# ──────────── MV ────────────


async def video_url(hash_: str) -> str:
    body = await request("/video/url", {"hash": hash_})
    data = (body or {}).get("data") or {}
    if not isinstance(data, dict):
        return ""
    key = hash_.lower()
    item = data.get(key) or {}
    if isinstance(item, dict):
        return str(item.get("downurl") or "")
    return ""


# ──────────── 设备注册 / 登录 ────────────


async def register_dev() -> str:
    """注册/获取设备 dfid，缓存到 api 模块（main.py 持久化到 plugin_data）。"""
    body = await request("/register/dev", {}, "get", inject_cookie=False)
    dfid = ((body or {}).get("data") or {}).get("dfid") or ""
    if dfid:
        set_device_cookie(f"dfid={dfid}")
    return dfid


async def qr_key() -> str:
    # KuGouMusicApi 对所有 200 响应缓存 2 分钟，二维码接口必须拼 timestamp 绕过
    body = await request("/login/qr/key", {"timestamp": int(time.time() * 1000)})
    return ((body or {}).get("data") or {}).get("qrcode") or ""


async def qr_create(key: str) -> dict:
    body = await request("/login/qr/create", {"key": key, "qrimg": "true"})
    data = (body or {}).get("data") or {}
    return {
        "url": data.get("url") or "",
        "base64": data.get("base64") or "",
    }


async def qr_check(key: str) -> dict:
    body = await request("/login/qr/check", {"key": key, "timestamp": int(time.time() * 1000)})
    data = (body or {}).get("data") or {}
    return {
        "status": data.get("status"),
        "token": data.get("token") or "",
        "userid": data.get("userid") or "",
        "nickname": data.get("nickname") or data.get("user_name") or "",
        "body": body,
    }


# ──────────── 用户（需登录） ────────────


async def user_detail(userid: str) -> dict:
    body = await request("/user/detail", {"userid": userid})
    data = (body or {}).get("data") or {}
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
    data = (body or {}).get("data") or {}
    # 实测结构：data.info[]（含 global_collection_id），兼容 list/plist
    pls = data.get("info") if isinstance(data, dict) else []
    if not pls:
        pls = data.get("list") if isinstance(data, dict) else []
    if not pls:
        pls = (body or {}).get("list") or []
    out = []
    for i, p in enumerate(pls):
        norm = _normalize_playlist(p, i)
        if norm:
            out.append(norm)
    return out


async def user_listen(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/listen", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data") or {}
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def user_history(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/history", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data") or {}
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


# ──────────── 新碟 / 推荐卡片 / 主题 ────────────


async def top_album(area: int = 0, page: int = 1, pagesize: int = 20) -> list:
    """新碟上架。type：1 华语 / 2 欧美 / 3 日本 / 4 韩国 / 0 推荐。"""
    params = {"type": area, "page": page, "pagesize": pagesize}
    # 实测：/top/album 与 /search 一样要求非空 token/userid 占位，否则 20010
    body = await request("/top/album", params, anon=True)
    chn = ((body or {}).get("data") or {}).get("chn") or []
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
    songs = ((body or {}).get("data") or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def theme_playlists(page: int = 1, pagesize: int = 20) -> list:
    body = await request("/theme/playlist", {"page": page, "pagesize": pagesize})
    lst = ((body or {}).get("data") or {}).get("theme_list") or []
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
    data = (body or {}).get("data") or {}
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def yueku() -> dict:
    """乐库：返回各区块条目数，供 #kg乐库 展示。"""
    body = await request("/yueku")
    info = ((body or {}).get("data") or {}).get("info") or {}
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
    lst = ((body or {}).get("data") or {}).get("list") or []
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
    lst = ((body or {}).get("data") or {}).get("list") or []
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
    lst = ((body or {}).get("data") or {}).get("lists") or []
    out = []
    for i, it in enumerate(lst):
        if not isinstance(it, dict):
            continue
        songs = it.get("song_list") or []
        norm_songs = []
        for j, s in enumerate(songs):
            norm = _normalize_song(s, j)
            if norm:
                norm_songs.append(norm)
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


# ──────────── 歌曲增强 ────────────


async def song_climax(hash_: str) -> dict:
    """歌曲高潮片段时间。"""
    body = await request("/song/climax", {"hash": hash_})
    d0 = ((body or {}).get("data") or [{}])[0] if (body or {}).get("data") else {}
    if not isinstance(d0, dict):
        return {}
    return {
        "start_ms": int(_num(d0.get("start_time"))),
        "end_ms": int(_num(d0.get("end_time"))),
        "duration_ms": int(_num(d0.get("timelength"))),
    }


async def ai_recommend(mixsongid: str, pagesize: int = 20) -> list:
    body = await request("/ai/recommend", {"album_audio_id": mixsongid, "pagesize": pagesize})
    songs = ((body or {}).get("data") or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def favorite_count(mixsongid: str) -> str:
    body = await request("/favorite/count", {"mixsongids": mixsongid})
    lst = ((body or {}).get("data") or {}).get("list") or []
    if lst and isinstance(lst[0], dict):
        return str(lst[0].get("count_text") or "")
    return ""


async def artist_albums(author_id, sort: str = "hot", page: int = 1, pagesize: int = 20) -> list:
    body = await request("/artist/albums", {"id": author_id, "sort": sort, "page": page, "pagesize": pagesize})
    data = (body or {}).get("data") or []
    out = []
    for i, a in enumerate(data):
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "index": i + 1,
                "id": a.get("albumid") or a.get("album_id") or 0,
                "name": a.get("album_name") or a.get("albumname") or "",
                "cover": str(a.get("sizable_cover") or a.get("cover") or a.get("img") or "").replace("{size}", "300"),
                "artist": a.get("author_name") or a.get("singername") or "",
                "publishDate": str(a.get("publish_date") or a.get("publishtime") or ""),
            }
        )
    return out


async def artist_lists(type_: int = 0, hotsize: int = 20) -> list:
    """歌手列表。type：0 全部 / 1 华语 / 2 欧美 / 3 日韩 / 4 其他 / 5 日本 / 6 韩国。"""
    body = await request("/artist/lists", {"type": type_, "hotsize": hotsize})
    info = ((body or {}).get("data") or {}).get("info") or []
    out = []
    idx = 0
    for sec in info:
        if not isinstance(sec, dict):
            continue
        for a in sec.get("singer") or []:
            if not isinstance(a, dict):
                continue
            idx += 1
            dy = a.get("dycover")
            if isinstance(dy, dict):  # dycover 可能是 {first_frame_image: url}
                cover = str(dy.get("first_frame_image") or a.get("avatar") or "")
            else:
                cover = str(dy or a.get("avatar") or "")
            out.append(
                {
                    "index": idx,
                    "id": a.get("singerid") or a.get("author_id") or 0,
                    "name": a.get("singername") or a.get("author_name") or "",
                    "cover": cover.replace("{size}", "300"),
                }
            )
    return out


# ──────────── 评论扩展 ────────────


async def comment_count(hash_: str) -> int:
    body = await request("/comment/count", {"hash": hash_})
    # 实测返回 {hash: count} 字典
    if isinstance(body, dict):
        for k, v in body.items():
            if k.lower().replace("-", "") == hash_.lower().replace("-", "") or len(k) == 32:
                return int(_num(v))
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


# ──────────── 账号扩展（需登录） ────────────


async def user_cloud(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/cloud", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data") or {}
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def user_purchased_songs(page: int = 1, pagesize: int = 30) -> list:
    body = await request("/user/purchased/songs", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data") or {}
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def user_purchased_albums(page: int = 1, pagesize: int = 20) -> list:
    body = await request("/user/purchased/albums", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data") or {}
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
    data = (body or {}).get("data") or {}
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
    data = (body or {}).get("data") or {}
    songs = data.get("song_list") if isinstance(data, dict) else []
    if not songs:
        songs = (body or {}).get("song_list") or []
    out = []
    for i, s in enumerate(songs):
        norm = _normalize_song(s, i)
        if norm:
            out.append(norm)
    return out


async def user_follow(page: int = 1, pagesize: int = 30) -> list:
    """用户关注歌手列表。"""
    body = await request("/user/follow", {"page": page, "pagesize": pagesize})
    data = (body or {}).get("data") or {}
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
    """上报听歌历史（需登录）。ot 为秒级时间戳。"""
    return await request("/playhistory/upload", {"mxid": mxid, "ot": int(time.time())})


# ──────────── 工具 ────────────


def extract_kugou_hash(text: str) -> str:
    """从酷狗分享链接/文本提取 hash 或 mixsongid。"""
    m = re.search(r"hash[=/]([0-9A-Fa-f]{32})", text or "")
    if m:
        return m.group(1).upper()
    m = re.search(r"mixsong(?:id)?[=/](\d+)", text or "")
    if m:
        return m.group(1)
    m = re.search(r"/song/([0-9A-Fa-f]{32})", text or "")
    if m:
        return m.group(1).upper()
    m = re.search(r"kugou\.com/mixsong/(\d+)", text or "")
    if m:
        return m.group(1)
    return ""


def is_kugou_link(text: str) -> bool:
    return bool(re.search(r"kugou\.com|kugou\.net", text or "", re.IGNORECASE))
