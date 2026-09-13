"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import _num

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
def _sub_dict(item: dict, key: str) -> dict:
    """取上游的嵌套子字典（``HQ`` / ``SQ`` / ``Res`` 等）。

    非 dict（字符串 / 列表 / 数字 / None）一律视为缺失：上游字段形态不稳定，
    直接 ``(item.get(key) or {}).get(...)`` 对真值非 dict 会抛 ``AttributeError``；
    该异常不是 ``ApiError``，``pick_song`` / ``chart`` 等的 ``except ApiError``
    接不住 → 用户拿不到错误回复，且写在 try 之后的 ``event.stop_event()``
    一并被跳过（消息继续流向后续管道/LLM）。
    """
    v = item.get(key)
    return v if isinstance(v, dict) else {}
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
        "hash_320": str(ainfo.get("hash_320") or item.get("hash_320") or _sub_dict(item, "HQ").get("Hash") or ""),
        "hash_flac": str(ainfo.get("hash_flac") or item.get("hash_flac") or _sub_dict(item, "SQ").get("Hash") or ""),
        "hash_high": str(
            ainfo.get("hash_high")
            or item.get("hash_high")
            or _sub_dict(item, "Res").get("Hash")
            or item.get("hash_flac")
            or ""
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
def _normalize_all(items, normalizer, limit: int | None = None) -> list:
    """「逐项归一化 → 丢弃 None」统一实现（18 处同构循环收敛到这里）。

    ``items`` 上游都已用 ``or []`` 兜住 None；``limit`` 用于只取前 N 条的场景
    （归一化时传入的序号仍按原始下标，与展开写法一致）。
    非 list（上游把字段换成 dict/字符串时的畸形返回）一律视为空列表。
    """
    if not isinstance(items, list):
        return []
    out = []
    for i, item in enumerate(items[:limit] if limit else items):
        norm = normalizer(item, i)
        if norm:
            out.append(norm)
    return out
def _data_of(body) -> dict:
    """取上游 body 的 ``data`` 子字典；非 dict（列表/字符串/数字/None）一律视为 ``{}``。

    原先各处写作 ``_data_of(body).get("xxx")``：``data`` 为真值
    但**不是** dict 时会抛 ``AttributeError``（非 ``ApiError``，handler 的
    ``except ApiError`` 接不住 → 用户零回复且消息继续流向后续管道）。
    """
    if not isinstance(body, dict):
        return {}
    d = body.get("data")
    return d if isinstance(d, dict) else {}
