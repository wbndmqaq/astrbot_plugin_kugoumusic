"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import ApiError, _num, request
from ._normalize import _data_of, _duration_text, _normalize_all, _normalize_song

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

    例外：``ApiError.timeout`` 为真的「总超时」**立即上抛**，不参与降级——
    超时说明 API 服务整体不可达/过慢，逐档重试只会把 20 秒超时放大成
    「阶梯长度 × 20 秒」（实测 8 次 ≈ 160 秒）；业务类错误（付费、无版权、
    该档 502）仍按原逻辑降档。
    """
    # 必须是 ``..quality``（core/quality.py）：拆包后 ``.quality`` 会解析到不存在的
    # core.api.quality；函数内导入不触发加载期报错，首次播放才炸
    from ..quality import hash_for_quality, quality_candidates

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
            if e.timeout:
                raise  # 总超时：立即失败，不再逐档重试
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
                if e.timeout:
                    raise  # 总超时同样不吞（否则又白等一个超时周期）
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
# ──────────── 歌曲增强 ────────────


async def song_climax(hash_: str) -> dict:
    """歌曲高潮片段时间。"""
    body = await request("/song/climax", {"hash": hash_})
    data = (body or {}).get("data")
    # 上游 data 可能是数组（单元素）也可能是按 hash 键控的对象，两者都兼容
    d0 = None
    if isinstance(data, list):
        d0 = data[0] if data else None
    elif isinstance(data, dict):
        d0 = data.get(hash_) or data.get(str(hash_).lower()) or data
    if not isinstance(d0, dict):
        return {}
    return {
        "start_ms": int(_num(d0.get("start_time"))),
        "end_ms": int(_num(d0.get("end_time"))),
        "duration_ms": int(_num(d0.get("timelength"))),
    }
async def ai_recommend(mixsongid: str, pagesize: int = 20) -> list:
    body = await request("/ai/recommend", {"album_audio_id": mixsongid, "pagesize": pagesize})
    songs = _data_of(body).get("song_list") or []
    return _normalize_all(songs, _normalize_song)
async def favorite_count(mixsongid: str) -> str:
    body = await request("/favorite/count", {"mixsongids": mixsongid})
    data = (body or {}).get("data")
    # 上游 data 可能是 {list: [...]} 也可能是裸数组，兼容两种
    lst = data.get("list") if isinstance(data, dict) else (data if isinstance(data, list) else [])
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
    info = _data_of(body).get("info") or []
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
