from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

from .quality import QUALITY_LABEL

# ──────────── 会话存储 ────────────


class SessionStore:
    _mem: dict = {}
    TTL = 600

    @classmethod
    def _key(cls, scope: str) -> str:
        return f"kg:song:{scope}"

    @classmethod
    async def get(cls, plugin, scope: str) -> dict | None:
        k = cls._key(scope)
        mem_val = cls._mem.get(str(scope))
        if mem_val:
            ts = mem_val.get("updatedAt") or 0
            if time.time() - ts < cls.TTL:
                return mem_val
            cls._mem.pop(str(scope), None)
        try:
            raw = await plugin.get_kv_data(k, None)
            if raw:
                if isinstance(raw, str):
                    raw = json.loads(raw)
                ts = raw.get("updatedAt") or 0
                if time.time() - ts < cls.TTL:
                    return raw
                await plugin.delete_kv_data(k)
        except Exception:
            pass
        return None

    @classmethod
    async def set(cls, plugin, scope: str, session: dict, ttl_sec: int = TTL) -> dict:
        data = {"group_id": scope, "updatedAt": time.time(), **session}
        cls._mem[str(scope)] = data
        try:
            await plugin.put_kv_data(cls._key(scope), json.dumps(data, ensure_ascii=False))
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
            f"{c['index']}. {c.get('nick') or '匿名'}（{fmt_count(c.get('likes') or 0)}赞）：{(c.get('content') or '')[:80]}"
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


def format_help_text(cfg: dict, version: str = "") -> str:
    api_hint = api_hint_for(cfg) if cfg.get("apiBase") else "⚠ API 未配置"
    lines = [
        f"🎵 酷狗音乐插件 v{version}" if version else "🎵 酷狗音乐插件",
        f"「{api_hint}」",
        "",
        "── 点歌播放 ──",
        "#kg点歌 关键词       搜索并列出歌曲",
        "#kg听N               播放列表第 N 首（可只发 #听N）",
        "#kg播放 关键词       搜索并直接播放第一首",
        "#kg歌词 关键词|hash  获取歌词",
        "#kg逐字歌词 关键词    KRC 逐字歌词",
        "#kg热搜              热搜榜",
        "",
        "── 发现音乐 ──",
        "#kg排行 [榜单名]     排行榜列表 / 查看具体榜单",
        "#kg歌手 关键词       歌手热门歌曲",
        "#kg专辑 关键词       专辑曲目",
        "#kg歌单 关键词|id    歌单曲目（VIP 歌单需登录）",
        "#kg评论 关键词       歌曲热评",
        "#kg相似 关键词|hash  相似歌曲",
        "#kg新歌              新歌速递",
        "#kg新碟 [华语/欧美/日本/韩国]  新碟上架",
        "#kg好歌 [精选/怀旧/热门/小众]  好歌精选卡片",
        "#kg主题歌单 [序号]  主题歌单 / 主题曲目",
        "#kg乐库              乐库概览",
        "#kg编辑精选          编辑精选专题",
        "#kg排行推荐          推荐榜单",
        "#kg历史日推 [序号]  历史每日推荐",
        "#kg精品歌单          精选歌单",
        "#kg歌单分类          歌单分类",
        "#kg搜索建议 关键词   关键词补全",
        "#kgMV 关键词         MV 详情与播放链接",
        "#kg高潮 关键词       歌曲高潮片段时间",
        "#kgAI推荐 关键词     AI 相似推荐",
        "#kg收藏 关键词       歌曲收藏数",
        "#kg版本 关键词       同一首歌的其他版本",
        "#kg歌手专辑 歌手     歌手的专辑列表",
        "#kg歌手列表 [分类]   歌手列表（华语/欧美/日韩等）",
        "#kg歌单评论 / #kg专辑评论 / #kg评论数 关键词",
        "#kg推荐 / #kg日推    每日推荐（需登录）",
        "#kg来首歌            随机来一首",
        "#kgFM                私人 FM（需登录）",
        "",
        "── 账号状态 ──",
        "#kg登录              扫码登录",
        "#kg状态 / #kgs       登录状态",
        "#kg登出              登出",
        "#kg我的歌单 / #kg最近 / #kg听歌排行  （需登录）",
        "#kg云盘 / #kg已购 / #kg等级 / #kg关注 歌手 / #kg取关 歌手 / #kg关注新歌  （需登录）",
        "",
        "── 管理（主人） ──",
        "#kg设置              设置面板",
        "#kg音质 <档位>       修改音质",
        "#kg api <地址>       修改 API 地址",
        "#kg测试              测试 API 连通",
        "",
        "── 自动解析 ──",
        "发送酷狗音乐分享链接（hash/mixsongid）自动解析播放",
        "",
        "Tips：未登录时 VIP 歌曲只能播放 60s 试听，登录后可播放全曲；播放后自动上报听歌历史。",
    ]
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
        "title": f"{song.get('name') or ''} - {song.get('artist') or ''}",
        "songName": _clean_name(song.get("name")),
        "singerName": _clean_name(song.get("artist")),
        "albumName": _clean_name(song.get("album")),
        "cover": song.get("cover") or "",
        "songId": song.get("hash") or song.get("id") or 0,
        "duration": song.get("duration") or "",
        "qualityLabel": quality_label or "",
        "payplay": bool(song.get("paid")),
        "trial": bool(song.get("trial")),
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
        "tip": tip or "发送 #kg歌单 歌单名 查看曲目",
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
    lines.append(tip or "发送 #kg歌单 歌单名 查看曲目")
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
        "tip": tip or "发送 #kg帮助 查看全部指令",
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
    lines.append(tip or "发送 #kg帮助 查看全部指令")
    return "\n".join(lines)


def build_lyric_card_data(song: dict, lines: list, line_count: int = 0) -> dict:
    return {
        "songName": _clean_name(song.get("name")),
        "singerName": _clean_name(song.get("artist")),
        "cover": song.get("cover") or "",
        "albumName": _clean_name(song.get("album")),
        "songId": song.get("hash") or song.get("id") or 0,
        "lines": lines,
        "lineCount": line_count,
        "tip": "歌词来自酷狗音乐",
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
                "index": c.get("index") or 0,
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
        "albumName": _clean_name(song.get("album")),
        "songId": song.get("hash") or song.get("id") or 0,
        "comments": items,
        "total": total or len(comments),
        "tip": "评论来自酷狗音乐",
    }


def build_help_card_data(version: str = "", cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    return {
        "version": version or "1.0.0",
        "statCommands": "45+",
        "statQuality": str(cfg.get("quality") or "auto"),
        "apiHint": api_hint_for(cfg),
        "tip": "未登录时 VIP 歌曲播放 60s 试听；#kg登录 后播放全曲；语音/文件投递可配置。",
        "sections": [
            {
                "title": "点歌播放",
                "tag": "全员可用",
                "items": [
                    {"name": "#kg点歌 关键词", "desc": "搜索并列出歌曲列表", "example": "#kg点歌 晴天"},
                    {"name": "#kg听N", "desc": "播放列表第 N 首", "example": "#kg听1"},
                    {"name": "#kg播放 关键词", "desc": "搜索并直接播放第一首", "example": "#kg播放 晴天"},
                    {"name": "#kg歌词 关键词|hash", "desc": "获取歌词", "example": "#kg歌词 晴天"},
                    {"name": "#kg热搜", "desc": "热搜榜", "example": "#kg热搜"},
                ],
            },
            {
                "title": "发现音乐",
                "tag": "全员可用",
                "items": [
                    {"name": "#kg排行 [榜单名]", "desc": "排行榜列表 / 具体榜单", "example": "#kg排行 TOP500"},
                    {"name": "#kg歌手 关键词", "desc": "歌手热门歌曲", "example": "#kg歌手 周杰伦"},
                    {"name": "#kg专辑 关键词", "desc": "专辑曲目", "example": "#kg专辑 叶惠美"},
                    {"name": "#kg歌单 关键词|id", "desc": "歌单曲目（VIP 需登录）", "example": "#kg歌单 华语"},
                    {"name": "#kg评论 关键词", "desc": "歌曲热评", "example": "#kg评论 晴天"},
                    {"name": "#kg相似 关键词|hash", "desc": "相似歌曲", "example": "#kg相似 晴天"},
                    {"name": "#kg新歌", "desc": "新歌速递", "example": "#kg新歌"},
                    {"name": "#kg新碟 [地区]", "desc": "新碟上架（华语/欧美/日本/韩国）", "example": "#kg新碟 华语"},
                    {"name": "#kg好歌 [卡片]", "desc": "好歌精选（精选/怀旧/热门/小众）", "example": "#kg好歌 热门"},
                    {"name": "#kg主题歌单 [序号]", "desc": "主题歌单列表 / 主题曲目", "example": "#kg主题歌单 1"},
                    {"name": "#kg乐库", "desc": "乐库概览", "example": "#kg乐库"},
                    {"name": "#kg编辑精选", "desc": "编辑精选专题", "example": "#kg编辑精选"},
                    {"name": "#kg排行推荐", "desc": "推荐榜单", "example": "#kg排行推荐"},
                    {"name": "#kg历史日推 [序号]", "desc": "历史每日推荐", "example": "#kg历史日推 1"},
                    {"name": "#kg精品歌单", "desc": "精选歌单", "example": "#kg精品歌单"},
                    {"name": "#kg歌单分类", "desc": "歌单分类列表", "example": "#kg歌单分类"},
                    {"name": "#kg搜索建议 关键词", "desc": "关键词补全", "example": "#kg搜索建议 晴天"},
                    {"name": "#kgMV 关键词", "desc": "MV 详情与播放链接", "example": "#kgMV 晴天"},
                    {"name": "#kg高潮 关键词", "desc": "歌曲高潮片段时间", "example": "#kg高潮 晴天"},
                    {"name": "#kgAI推荐 关键词", "desc": "AI 相似推荐", "example": "#kgAI推荐 晴天"},
                    {"name": "#kg收藏 关键词", "desc": "歌曲收藏数", "example": "#kg收藏 晴天"},
                    {"name": "#kg版本 关键词", "desc": "同一首歌的其他版本", "example": "#kg版本 晴天"},
                    {"name": "#kg歌手专辑 歌手", "desc": "歌手的专辑列表", "example": "#kg歌手专辑 周杰伦"},
                    {
                        "name": "#kg歌手列表 [分类]",
                        "desc": "歌手列表（华语/欧美/日韩等）",
                        "example": "#kg歌手列表 华语",
                    },
                    {"name": "#kg歌单评论 / #kg专辑评论", "desc": "歌单/专辑热评", "example": "#kg专辑评论 叶惠美"},
                    {"name": "#kg评论数 关键词", "desc": "歌曲评论数", "example": "#kg评论数 晴天"},
                    {"name": "#kg来首歌", "desc": "随机来一首", "example": "#kg来首歌"},
                    {"name": "#kgFM", "desc": "私人 FM（需登录）", "example": "#kgFM"},
                ],
            },
            {
                "title": "推荐",
                "tag": "需登录",
                "items": [
                    {"name": "#kg推荐 / #kg日推", "desc": "每日推荐", "example": "#kg日推"},
                ],
            },
            {
                "title": "账号",
                "tag": "需登录",
                "items": [
                    {"name": "#kg我的歌单", "desc": "我创建/收藏的歌单", "example": "#kg我的歌单"},
                    {"name": "#kg最近", "desc": "最近播放歌曲", "example": "#kg最近"},
                    {"name": "#kg听歌排行", "desc": "听歌排行", "example": "#kg听歌排行"},
                    {"name": "#kg云盘", "desc": "我的云盘歌曲", "example": "#kg云盘"},
                    {"name": "#kg已购", "desc": "已购单曲/专辑", "example": "#kg已购"},
                    {"name": "#kg等级", "desc": "听歌等级", "example": "#kg等级"},
                    {"name": "#kg关注 / #kg取关 歌手", "desc": "关注/取关歌手", "example": "#kg关注 周杰伦"},
                    {"name": "#kg关注新歌", "desc": "关注歌手的上新", "example": "#kg关注新歌"},
                ],
            },
            {
                "title": "账号状态",
                "tag": "全员可用",
                "items": [
                    {"name": "#kg登录", "desc": "酷狗 App 扫码登录", "example": "#kg登录"},
                    {"name": "#kgqq登录", "desc": "QQ 扫码授权登录", "example": "#kgqq登录"},
                    {"name": "#kg状态 / #kgs", "desc": "查看登录状态", "example": "#kgs"},
                    {"name": "#kg登出", "desc": "登出", "example": "#kg登出"},
                ],
            },
            {
                "title": "管理",
                "tag": "主人",
                "items": [
                    {"name": "#kg设置", "desc": "设置面板", "example": "#kg设置"},
                    {
                        "name": "#kg音质 <档位>",
                        "desc": "修改音质（auto/viper_tape/viper_clear/super/high/flac/320/128）",
                        "example": "#kg音质 high",
                    },
                    {"name": "#kg api <地址>", "desc": "修改 API 地址", "example": "#kg api http://127.0.0.1:3000"},
                    {"name": "#kg测试", "desc": "测试 API 连通", "example": "#kg测试"},
                ],
            },
            {
                "title": "自动解析",
                "tag": "自动",
                "items": [
                    {
                        "name": "酷狗链接",
                        "desc": "kugou.com 歌曲/歌单链接自动解析播放",
                        "example": "https://www.kugou.com/song/#hash=xxx",
                    },
                ],
            },
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
        "vip": 1 if vip_label else 0,
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
        "maxList": int(cfg.get("maxList") or 10),
        "loginStatus": (f"有 Cookie · uid={uid}" if uid else ("默认账号" if default_cookie else "未登录")),
        "loggedIn": bool(uid or default_cookie),
        "toggles": [
            {"name": "点歌", "on": cfg.get("enableSongRequest", True) is not False},
            {"name": "语音", "on": cfg.get("sendVocal", True) is not False},
            {"name": "文件", "on": cfg.get("uploadFile", True) is not False},
            {"name": "卡片渲染", "on": cfg.get("renderListCard", True) is not False},
            {"name": "扫码登录", "on": cfg.get("qrLoginEnable", True) is not False},
            {"name": "试听降级", "on": cfg.get("trialFallback", True) is not False},
        ],
        "commands": [
            {"cmd": "#kg音质 &lt;档位&gt;", "desc": "修改音质"},
            {"cmd": "#kg api &lt;地址&gt;", "desc": "修改 API 地址"},
        ],
        "tip": "设置修改即时生效，无需重启",
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
