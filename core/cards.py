from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

from .api import cfg_int
from .help_data import HELP_SECTIONS
from .messages import LYRICS_SOURCE, TIP_HELP, TIP_PLAYLIST_VIEW
from .quality import QUALITY_LABEL

# ──────────── 会话存储 ────────────


class SessionStore:
    _mem: dict = {}
    TTL = 600
    # 内存缓存条数上限：防止大量群/私聊会话长期驻留导致无界增长。
    # 超过上限时按 updatedAt 淘汰最旧条目（数据已持久化到 KV，淘汰不丢数据）。
    MAX_MEM = 512

    @classmethod
    def _evict_if_needed(cls) -> None:
        if len(cls._mem) <= cls.MAX_MEM:
            return
        overflow = len(cls._mem) - cls.MAX_MEM
        oldest = sorted(
            cls._mem.items(), key=lambda kv: kv[1].get("updatedAt") or 0
        )[:overflow]
        for k, _ in oldest:
            cls._mem.pop(k, None)

    @classmethod
    def _mem_key(cls, scope: str, kind: str) -> str:
        return f"{kind}:{scope}"

    @classmethod
    def _key(cls, scope: str, kind: str = "songs") -> str:
        """KV 键：点歌列表沿用既有 kg:song:{scope}（老数据不失效），其余按 kind 分桶。

        分桶是必须的：#kg主题歌单 / #kg历史日推 与点歌列表若共用同一个 key，
        写前者会冲掉用户在用的点歌列表（随后 #kg听N 因 type 不匹配而静默无响应）。
        """
        return f"kg:song:{scope}" if kind == "songs" else f"kg:sess:{kind}:{scope}"

    @classmethod
    async def get(cls, plugin, scope: str, kind: str = "songs") -> dict | None:
        k = cls._key(scope, kind)
        mem_k = cls._mem_key(scope, kind)
        mem_val = cls._mem.get(mem_k)
        if mem_val:
            ts = mem_val.get("updatedAt") or 0
            if time.time() - ts < cls.TTL:
                return mem_val
            cls._mem.pop(mem_k, None)
        try:
            raw = await plugin.get_kv_data(k, None)
            if raw:
                if isinstance(raw, str):
                    raw = json.loads(raw)
                ts = raw.get("updatedAt") or 0
                if time.time() - ts < cls.TTL:
                    # 命中 KV 时回填内存缓存，避免后续每次仍读 KV
                    cls._mem[mem_k] = raw
                    cls._evict_if_needed()
                    return raw
                await plugin.delete_kv_data(k)
        except Exception:
            pass
        return None

    @classmethod
    async def set(cls, plugin, scope: str, session: dict, kind: str = "songs") -> dict:
        # updatedAt 放在 **session 之后：调用方常传「dict(旧会话)」（读会话 → 改字段 → 写回，
        # 例如 choose_song 把 action 复位为 play），旧会话里带着旧 updatedAt，展开顺序若让
        # session 在后就会覆盖刚生成的新时间戳 → TTL 永远锚定首次创建时刻，会话在第 600 秒
        # 准时失效且重复操作不续期（用户会遇到一次静默无响应）。新时间戳必须胜出。
        data = {"group_id": scope, **session, "updatedAt": time.time()}
        cls._mem[cls._mem_key(scope, kind)] = data
        # 先写内存再落盘：插入后才淘汰，避免 _mem 突破 MAX_MEM
        cls._evict_if_needed()
        # 同 scope 并发写时 KV 可能落到较旧那份（后写覆盖先写）。这里不做
        # 「读-比 updatedAt-再写」：读改写不是原子操作，多一次 KV 读盘也换不来
        # 严格顺序保证，反而给每次列表写入加一次往返；会话数据本就是最近一次
        # 列表的覆盖语义，保持现状。
        try:
            await plugin.put_kv_data(cls._key(scope, kind), json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
        return data


# ──────────── 隐私脱敏 ────────────


def mask_api_base(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return "****"
    try:
        parsed = urlparse(u)
        host = parsed.hostname or ""
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host) or ":" in host:
            masked_host = "***"  # IPv4/IPv6 整体打码
        else:
            last_dot = host.rfind(".")
            masked_host = "***" + (host[last_dot:] if last_dot > 0 else "")
        port = f":{parsed.port}" if parsed.port else ""
        path_part = parsed.path if (parsed.path and parsed.path != "/") else ""
        return f"{parsed.scheme}://{masked_host}{port}{path_part}"
    except Exception:
        return "****"


def api_hint_for(cfg: dict) -> str:
    if not cfg.get("apiBase"):
        return "API 未配置"
    return f"API · {mask_api_base(cfg['apiBase']).replace('https://', '').replace('http://', '')}"


def fmt_count(n: float) -> str:
    n = int(n or 0)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


# ──────────── 文本格式化（纯文本兜底） ────────────


def _pay_tag(s: dict) -> str:
    if s.get("paid"):
        return " [VIP/付费]"
    return ""


def format_song_list(lst: list, title: str, tip: str = "") -> str:
    if not isinstance(lst, list) or not lst:
        return f"♫ {title}\n\n📭 暂无数据\n可能原因：\n1. API 未启动或网络异常\n2. 账号未登录（需要 #kg登录）\n3. 请求超时，请稍后重试"
    lines = [f"♫ {title}"]
    for i, s in enumerate(lst):
        idx = i + 1
        dur = f" ({s['duration']})" if s.get("duration") else ""
        lines.append(f"{idx}. {s.get('name') or '未知'} - {s.get('artist') or '未知'}{_pay_tag(s)}{dur}")
    lines.append(f"\n发送 #kg听序号 播放（共{len(lst)}首）")
    if tip:
        lines.append(tip)
    return "\n".join(lines)


def format_hot_text(lst: list) -> str:
    lines = []
    for i, h in enumerate(lst):
        reason = f"  {h.get('reason')}" if h.get("reason") and h.get("reason") != h.get("word") else ""
        lines.append(f"{i + 1}. {h.get('word') or ''}{reason}")
    return "\n".join(lines) or "📭 暂无热搜数据"


def format_lyric_text(song: dict, lines: list) -> str:
    head = f"♪ {song.get('name') or ''} - {song.get('artist') or ''}"
    if not lines:
        return f"{head}\n\n（暂无歌词）"
    return head + "\n" + "\n".join(lines)


def format_detail_text(song: dict, play: dict | None = None, tip: str = "") -> str:
    quality_label = ""
    if play and (play.get("qualityLabel") or play.get("quality")):
        quality_label = play.get("qualityLabel") or QUALITY_LABEL.get(play.get("quality") or "", "")
    lines = [
        f"♪ {song.get('name') or '未知'} - {song.get('artist') or '未知'}{_pay_tag(song)}",
        f"专辑：{song.get('album') or ''}" if song.get("album") else "",
        f"音质：{quality_label}" if quality_label else "",
    ]
    if tip:
        lines.append(tip)
    return "\n".join(x for x in lines if x)


def format_comment_text(song: dict, comments: list) -> str:
    lines = [f"♪ {song.get('name') or ''} - {song.get('artist') or ''} 热评"]
    for c in comments[:15]:
        lines.append(
            f"{c.get('index') or 0}. {c.get('nick') or '匿名'}（{fmt_count(c.get('likes') or 0)}赞）：{(c.get('content') or '')[:80]}"
        )
    return "\n".join(lines) or "📭 暂无评论"


def format_status_text(status: dict) -> str:
    lines = []
    if status.get("loggedIn"):
        lines.append(f"✅ 已登录：{status.get('nickname') or ''}")
        if status.get("uin"):
            lines.append(f"账号：{status['uin']}")
        if status.get("vipLabel"):
            lines.append(f"会员：{status['vipLabel']}")
    else:
        lines.append("❌ 未登录")
        lines.append("发送 #kg登录 扫码登录")
    if status.get("apiBase"):
        lines.append(f"API：{mask_api_base(status['apiBase'])}")
    if status.get("quality"):
        lines.append(f"音质：{status['quality']}")
    if status.get("keyStatus"):
        lines.append(status["keyStatus"])
    return "\n".join(lines)


def _disp_width(s: str) -> int:
    """CJK 按 2 列宽计的对齐宽度。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad_name(name: str) -> str:
    """命令名对齐到统一列（至少 2 个空格分隔），长名只留 2 空格。"""
    return " " * max(21 - _disp_width(name), 2)


def format_help_text(cfg: dict, version: str = "") -> str:
    """纯文本帮助：渲染 core/help_data.py 的唯一数据表。"""
    api_hint = api_hint_for(cfg) if cfg.get("apiBase") else "⚠ API 未配置"
    lines = [
        f"🎵 酷狗音乐插件 v{version}" if version else "🎵 酷狗音乐插件",
        f"「{api_hint}」",
        "",
    ]
    for sec in HELP_SECTIONS:
        if not sec.get("plain_no_head"):
            lines.append(f"── {sec.get('plain_title') or sec['title']} ──")
        for it in sec["items"]:
            if it.get("plain_skip"):
                continue
            if it.get("plain"):
                lines.append(it["plain"])
                continue
            name = it.get("plain_name") or it["name"]
            desc = it.get("plain_desc") or it["desc"]
            lines.append(f"{name}{_pad_name(name)}{desc}")
        lines.append("")
    lines.append("Tips：未登录时 VIP 歌曲只能播放 60s 试听，登录后可播放全曲；播放后自动上报听歌历史。")
    return "\n".join(lines)

# ──────────── 卡片数据构建 ────────────


def _clean_name(s) -> str:
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()


def build_list_card_data(keyword: str, songs: list, options: dict | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    options = options or {}
    return {
        "keyword": keyword or "歌曲列表",
        "total": len(songs),
        "quality": str(cfg.get("quality") or "auto").upper(),
        "apiHint": api_hint_for(cfg),
        "songs": [
            {
                "index": i + 1,
                "songName": _clean_name(s.get("name")),
                "singerName": _clean_name(s.get("artist")),
                "albumName": _clean_name(s.get("album")),
                "cover": s.get("cover") or "",
                "duration": s.get("duration") or "",
                "payplay": bool(s.get("paid")),
            }
            for i, s in enumerate(songs)
        ],
        "tip": options.get("tip") or "发送 #kg听序号 播放（会话内也可 #听序号）；列表约 10 分钟内有效",
    }


def build_detail_card_data(song: dict, quality_label: str = "", source: str = "", tip: str = "") -> dict:
    return {
        "songName": _clean_name(song.get("name")),
        "singerName": _clean_name(song.get("artist")),
        "albumName": _clean_name(song.get("album")),
        "cover": song.get("cover") or "",
        "songId": song.get("hash") or song.get("id") or 0,
        "duration": song.get("duration") or "",
        "qualityLabel": quality_label or "",
        "payplay": bool(song.get("paid")),
        "source": source or "",
        "tip": tip or "",
    }


def build_playlist_card_data(
    title: str,
    playlists: list,
    *,
    subtitle: str = "",
    tip: str = "",
    tip_title: str = "提示",
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or {}
    items = []
    for p in playlists:
        items.append(
            {
                "index": p.get("index") or 0,
                "name": _clean_name(p.get("name")),
                "creator": _clean_name(p.get("creator")),
                "cover": p.get("cover") or "",
                "trackCount": int(p.get("songCount") or p.get("trackCount") or 0),
                "playCountText": f"{fmt_count(p.get('playCount') or 0)}播放",
            }
        )
    return {
        "title": title or "歌单列表",
        "subtitle": subtitle or "酷狗音乐歌单",
        "total": len(items),
        "totalPlay": fmt_count(sum(int(p.get("playCount") or 0) for p in playlists)),
        "items": items,
        "tip": tip or TIP_PLAYLIST_VIEW,
        "tipTitle": tip_title,
        "apiHint": api_hint_for(cfg),
    }


def format_playlist_text(title: str, playlists: list, tip: str = "") -> str:
    lines = [f"♫ {title}"]
    for p in playlists:
        lines.append(
            f"{p.get('index') or 0}. {p.get('name') or '未知'}（{fmt_count(p.get('playCount') or 0)}播放 · {p.get('songCount') or p.get('trackCount') or 0}首）"
        )
    lines.append("")
    lines.append(tip or TIP_PLAYLIST_VIEW)
    return "\n".join(lines)


def build_generic_card_data(
    title: str,
    items: list,
    *,
    subtitle: str = "",
    tip: str = "",
    tip_title: str = "提示",
    stat_mid: str = "",
    stat_mid_label: str = "",
    cfg: dict | None = None,
) -> dict:
    stat_mid = stat_mid or str(len(items))
    stat_mid_label = stat_mid_label or "条"
    cfg = cfg or {}
    out = []
    for i, it in enumerate(items):
        out.append(
            {
                "index": i + 1,
                "name": _clean_name(it.get("name") or it.get("main") or ""),
                "sub": _clean_name(it.get("sub") or ""),
                "tag": _clean_name(it.get("tag") or ""),
                "cover": it.get("cover") or "",
            }
        )
    return {
        "title": title or "列表",
        "subtitle": subtitle or "酷狗音乐",
        "total": len(out),
        "statMid": stat_mid,
        "statMidLabel": stat_mid_label,
        "items": out,
        "tip": tip or TIP_HELP,
        "tipTitle": tip_title,
        "apiHint": api_hint_for(cfg),
    }


def format_generic_text(title: str, items: list, tip: str = "") -> str:
    lines = [f"♫ {title}"]
    for i, it in enumerate(items):
        name = it.get("name") or it.get("main") or ""
        sub = " · ".join(x for x in (it.get("sub"), it.get("tag")) if x)
        line = f"{i + 1}. {name}"
        if sub:
            line += f"（{sub}）"
        lines.append(line)
    lines.append("")
    lines.append(tip or TIP_HELP)
    return "\n".join(lines)


def build_lyric_card_data(song: dict, lines: list, line_count: int = 0) -> dict:
    return {
        "songName": _clean_name(song.get("name")),
        "singerName": _clean_name(song.get("artist")),
        "cover": song.get("cover") or "",
        "lines": lines,
        "lineCount": line_count,
        "tip": LYRICS_SOURCE,
    }


def build_hot_card_data(items: list, title: str = "热搜榜") -> dict:
    return {
        "title": title,
        "subtitle": "酷狗音乐热搜",
        "total": len(items),
        "items": [
            {"index": i + 1, "word": h.get("word") or "", "hot": h.get("reason") or ""} for i, h in enumerate(items)
        ],
        "tip": "发送 #kg搜索建议 关键词 获取补全建议",
    }


def clean_comment_text(s: str) -> str:
    if not s:
        return ""
    s = str(s)
    s = re.sub(r"\[em\]e\d+\[/em\]", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = s.replace("\\r\\n", " ").replace("\\n", " ").replace("\r\n", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_comment_card_data(song: dict, comments: list, total: int = 0) -> dict:
    items = []
    for c in comments[:20]:
        nick = c.get("nick") or ""
        items.append(
            {
                "nick": nick,
                "avatar": c.get("avatar") or "",
                "avatarPh": nick[:1] if nick else "♪",
                # 酷狗评论时间直接是 "2025-12-26 19:46:04" 字符串
                "time": str(c.get("time") or "")[:10],
                "likes": fmt_count(c.get("likes") or 0),
                "content": clean_comment_text(c.get("content")),
                "hot": bool(c.get("hot")),
            }
        )
    return {
        "songName": _clean_name(song.get("name")),
        "singerName": _clean_name(song.get("artist")),
        "cover": song.get("cover") or "",
        "comments": items,
        "total": total or len(comments),
        "tip": "评论来自酷狗音乐",
    }


def build_help_card_data(version: str = "", cfg: dict | None = None, stat_commands: str = "") -> dict:
    """帮助卡片：渲染 core/help_data.py 的唯一数据表（与纯文本同源）。"""
    cfg = cfg or {}
    return {
        # 版本号取不到时如实展示 "?"，不伪造 1.0.0
        "version": version or "?",
        # 指令数由调用方传入真实路由数（零值时不展示该统计项，不写死假数字）
        "statCommands": stat_commands or "",
        "statQuality": str(cfg.get("quality") or "auto"),
        "apiHint": api_hint_for(cfg),
        "tip": "未登录时 VIP 歌曲播放 60s 试听；#kg登录 后播放全曲；语音/文件投递可配置。",
        "sections": [
            {
                "title": sec["title"],
                "tag": sec["tag"],
                "items": [
                    {"name": it["name"], "desc": it["desc"], "example": it.get("example") or ""}
                    for it in sec["items"]
                ],
            }
            for sec in HELP_SECTIONS
        ],
    }


def build_status_card_data(status: dict) -> dict:
    nickname = status.get("nickname") or ""
    vip_label = status.get("vipLabel") or ""
    return {
        "title": "酷狗音乐 · 登录状态",
        "loggedIn": bool(status.get("loggedIn")),
        "badge": status.get("badge") or ("已登录" if status.get("loggedIn") else "未登录"),
        "nickname": nickname,
        "avatar": status.get("avatar") or "",
        "avatarPh": nickname[:1] or "♪",
        "uin": status.get("uin") or "",
        "level": status.get("level") or "",
        "vipLabel": vip_label,
        "vipGold": bool(status.get("vipGold")),
        "vipLevel": int(status.get("vipLevel") or 0),
        "apiBase": mask_api_base(status.get("apiBase") or ""),
        "keyStatus": status.get("keyStatus") or "",
        "quality": status.get("quality") or "",
        "tip": "发送 #kg登录 扫码登录；#kg音质 <档位> 修改音质",
    }


def build_settings_card_data(cfg: dict, uid: str = "") -> dict:
    default_cookie = str(cfg.get("defaultCookie") or "")
    cookie_tail = default_cookie[-4:] if len(default_cookie) >= 4 else "****"
    q = str(cfg.get("quality") or "auto")
    return {
        "title": "酷狗音乐 · 插件设置",
        "apiBase": mask_api_base(cfg.get("apiBase") or "") or "未配置",
        "apiHint": api_hint_for(cfg),
        "cookieStatus": f"已配置（***{cookie_tail}）" if default_cookie else "未配置",
        "quality": QUALITY_LABEL.get(q, q),
        "maxList": cfg_int(cfg, "maxList", 10),
        "loginStatus": (f"有 Cookie · uid={uid}" if uid else ("默认账号" if default_cookie else "未登录")),
        "toggles": [
            {"name": "点歌", "on": cfg.get("enableSongRequest", True) is not False},
            {"name": "语音", "on": cfg.get("sendVocal", True) is not False},
            {"name": "文件", "on": cfg.get("uploadFile", True) is not False},
            {"name": "卡片渲染", "on": cfg.get("renderListCard", True) is not False},
            {"name": "扫码登录", "on": cfg.get("qrLoginEnable", True) is not False},
            {"name": "试听降级", "on": cfg.get("trialFallback", True) is not False},
        ],
        "commands": [
            # 写裸 < >，交给模板的 autoescape 处理；预转义实体会被二次转义成 &amp;lt;
            {"cmd": "#kg音质 <档位>", "desc": "修改音质"},
            {"cmd": "#kg api <地址>", "desc": "修改 API 地址"},
        ],
    }


def format_settings_text(cfg: dict, uid: str = "") -> str:
    default_cookie = str(cfg.get("defaultCookie") or "")
    cookie_tail = default_cookie[-4:] if len(default_cookie) >= 4 else "****"
    q = str(cfg.get("quality") or "auto")
    lines = [
        "🎵 酷狗音乐插件设置",
        f"API：{mask_api_base(cfg.get('apiBase') or '') or '未配置'}",
        f"默认Cookie：{'已配置（***' + cookie_tail + '）' if default_cookie else '未配置'}",
        f"点歌：{'开' if cfg.get('enableSongRequest', True) is not False else '关'}",
        f"音质：{QUALITY_LABEL.get(q, q)}",
        f"试听降级：{'开' if cfg.get('trialFallback', True) is not False else '关'}",
        (
            f"语音：{'开' if cfg.get('sendVocal', True) is not False else '关'}　"
            f"文件：{'开' if cfg.get('uploadFile', True) is not False else '关'}"
        ),
        f"卡片渲染：{'开' if cfg.get('renderListCard', True) is not False else '关'}",
        f"列表上限：{cfg.get('maxList', 10)}　扫码登录：{'开' if cfg.get('qrLoginEnable', True) is not False else '关'}",
        f"登录：{('有 Cookie · uid=' + uid) if uid else (('默认账号') if default_cookie else '未登录')}",
        "",
        "可修改：#kg音质 <档位> / #kg api <地址>",
    ]
    return "\n".join(lines)
