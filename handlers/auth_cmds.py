from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image

if TYPE_CHECKING:
    from ..core.service import MusicService

try:
    from ..core import api as kgapi
    from ..core import cards as cardlib
    from ..core.api import ApiError
except ImportError:
    from core import api as kgapi
    from core import cards as cardlib
    from core.api import ApiError
from .base import Route


async def start_qr_login(service: MusicService, event: AstrMessageEvent):
    """#kg登录：扫码登录酷狗账号"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    if cfg.get("qrLoginEnable") is False:
        await service.reply(event, "扫码登录已在配置中关闭")
        event.stop_event()
        return
    user_key = service.user_key(event)
    service.stop_poll(user_key)
    try:
        await service.reply(event, "正在获取酷狗登录二维码…")
        key = await kgapi.qr_key()
        if not key:
            await service.reply(event, "获取二维码失败：无法获取 qrcode，请检查 API 服务")
            event.stop_event()
            return
        info = await kgapi.qr_create(key)
        qrurl = info.get("url") or ""
        qrimg = info.get("base64") or ""
        tip_text = "请使用酷狗音乐 App 扫码登录\n二维码约 5 分钟内有效"
        qr_path = await service.save_qr_image(qrimg) if qrimg else None
        img_sent = False
        if qr_path:
            try:
                await service.send_chain(event, Image.fromFileSystem(qr_path), service.plain(tip_text))
                img_sent = True
            except Exception:
                pass
            asyncio.get_running_loop().call_later(120, lambda: service.safe_unlink(qr_path))
        if not img_sent:
            await service.reply(event, tip_text + (f"\n或打开链接扫码：{qrurl}" if qrurl else ""))
        service.start_poll(event, key, 300)
    except ApiError as err:
        await service.reply(event, f"扫码登录失败：{err}")
    event.stop_event()


async def start_qq_qr_login(service: MusicService, event: AstrMessageEvent):
    """#kgqq登录：通过 QQ 扫码登录绑定酷狗账号"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    if cfg.get("qrLoginEnable") is False:
        await service.reply(event, "扫码登录已在配置中关闭")
        event.stop_event()
        return
    user_key = service.user_key(event)
    service.stop_poll(user_key)
    try:
        await service.reply(event, "正在获取 QQ 登录二维码…")
        info = await kgapi.login_qq_qr_create()
        qrimg = info.get("qrcode") or ""
        if not qrimg:
            await service.reply(event, "获取 QQ 二维码失败，请检查 API 服务")
            event.stop_event()
            return
        tip_text = "请使用手机 QQ 扫码授权登录酷狗\n二维码约 2 分钟内有效"
        qr_path = await service.save_qr_image(qrimg)
        img_sent = False
        if qr_path:
            try:
                await service.send_chain(event, Image.fromFileSystem(qr_path), service.plain(tip_text))
                img_sent = True
            except Exception:
                pass
            asyncio.get_running_loop().call_later(120, lambda: service.safe_unlink(qr_path))
        if not img_sent:
            await service.reply(event, tip_text)
        service.start_qq_poll(event, info, 180)
    except ApiError as err:
        await service.reply(event, f"QQ 扫码登录失败：{err}")
    event.stop_event()


async def login_status_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg状态 / #kgs：查看登录状态"""
    if not service.cfg().get("enable", True):
        return
    await service.send_status(event)
    event.stop_event()


async def logout(service: MusicService, event: AstrMessageEvent):
    """#kg登出：清除酷狗登录 Cookie"""
    try:
        if service.plugin.config.get("defaultCookie"):
            service.plugin.config["defaultCookie"] = ""
            service.plugin.config["defaultUid"] = ""
            service.plugin.config.save_config()
            await service.reply(event, "已登出酷狗账号，并清除插件配置中的默认 Cookie")
        else:
            await service.reply(event, "当前未配置登录 Cookie")
    except Exception as err:
        await service.reply(event, f"登出失败：{err}")
    event.stop_event()


async def cloud(service: MusicService, event: AstrMessageEvent):
    """#kg云盘：我的云盘歌曲（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    try:
        songs = await kgapi.user_cloud(pagesize=30)
        if not songs:
            await service.reply(event, "云盘暂无歌曲")
            event.stop_event()
            return
        await service.list_to_session(event, "我的云盘", songs[:20])
    except ApiError as err:
        service.log_warn(f"云盘失败: {err}")
        await service.reply(event, f"获取云盘失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def purchased(service: MusicService, event: AstrMessageEvent):
    """#kg已购：已购单曲/专辑（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    try:
        songs = await kgapi.user_purchased_songs(pagesize=20)
        albums = await kgapi.user_purchased_albums(pagesize=10)
        lines = []
        if songs:
            lines.append(f"已购单曲（{len(songs)}）:")
            lines.extend(f"{i + 1}. {s['name']} - {s['artist']}" for i, s in enumerate(songs[:15]))
        if albums:
            lines.append(f"已购专辑（{len(albums)}）:")
            lines.extend(f"{i + 1}. {a['name']} - {a['artist']}" for i, a in enumerate(albums[:10]))
        if not lines:
            await service.reply(event, "暂无已购内容")
        else:
            await service.reply(event, "🎵 已购内容\n" + "\n".join(lines))
    except ApiError as err:
        service.log_warn(f"已购失败: {err}")
        await service.reply(event, f"获取已购失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def grade(service: MusicService, event: AstrMessageEvent):
    """#kg等级：听歌等级（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    try:
        g = await kgapi.user_grade_info()
        if not g:
            await service.reply(event, "暂无听歌等级数据")
            event.stop_event()
            return
        hours = g["dSec"] / 3600
        lines = [
            f"🎧 听歌等级：Lv.{g['grade']}",
            f"累计听歌时长：{hours:.1f} 小时（{g['dSec']} 秒）",
            f"当前积分：{g['currentPoint']}",
        ]
        if g.get("nextGrade"):
            lines.append(f"距 Lv.{g['nextGrade']} 还差 {max(0, g['nextGradePoint'] - g['currentPoint'])} 分")
        await service.reply(event, "\n".join(lines))
    except ApiError as err:
        service.log_warn(f"等级失败: {err}")
        await service.reply(event, f"获取听歌等级失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def follow_toggle(service: MusicService, event: AstrMessageEvent):
    """#kg关注 歌手 / #kg取关 歌手：关注/取关歌手（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    m = re.match(r"^#?(?:kg|KG)\s*(关注|取关|取消关注)\s+(.+)$", event.message_str.strip(), re.IGNORECASE)
    action = m.group(1) if m else ""
    kw = (m.group(2).strip() if m else "").strip()
    try:
        a = await service.resolve_artist(kw)
        if not a:
            await service.reply(event, f"没有搜到歌手「{kw}」")
            event.stop_event()
            return
        if action in ("取关", "取消关注"):
            await kgapi.artist_unfollow(a["id"])
            await service.reply(event, f"已取消关注：{a['name']}")
        else:
            await kgapi.artist_follow(a["id"])
            await service.reply(event, f"已关注：{a['name']}")
    except ApiError as err:
        service.log_warn(f"关注操作失败: {err}")
        await service.reply(event, f"操作失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def follow_newsongs(service: MusicService, event: AstrMessageEvent):
    """#kg关注新歌：关注的歌手新歌（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    try:
        songs = await kgapi.artist_follow_newsongs(pagesize=30)
        if not songs:
            await service.reply(event, "暂无关注歌手新歌")
            event.stop_event()
            return
        await service.list_to_session(event, "关注歌手新歌", songs[:20])
    except ApiError as err:
        service.log_warn(f"关注新歌失败: {err}")
        await service.reply(event, f"获取关注新歌失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def follow_list(service: MusicService, event: AstrMessageEvent):
    """#kg关注列表：我关注的歌手（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    try:
        artists = await kgapi.user_follow(pagesize=30)
        if not artists:
            await service.reply(event, "暂无关注歌手")
            event.stop_event()
            return
        data = cardlib.build_generic_card_data(
            "我关注的歌手",
            [{"name": a["name"], "cover": a.get("cover") or ""} for a in artists],
            subtitle=f"共 {len(artists)} 位",
            tip="发送 #kg歌手 歌手名 查看热门歌曲；#kg取关 歌手名 取关",
            cfg=service.cfg(),
        )
        await service.reply_card_or_text(
            event,
            tpl_name="kg-generic",
            data=data,
            format_text=lambda d: cardlib.format_generic_text(
                "我关注的歌手",
                [{"name": a["name"]} for a in artists],
                tip="发送 #kg歌手 歌手名 查看热门歌曲；#kg取关 歌手名 取关",
            ),
        )
    except ApiError as err:
        service.log_warn(f"关注列表失败: {err}")
        await service.reply(event, f"获取关注列表失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def my_playlist(service: MusicService, event: AstrMessageEvent):
    """#kg我的歌单：我创建/收藏的歌单（需登录）"""
    if not service.cfg().get("enable", True):
        return
    uid = await service.get_uid()
    if not uid:
        await service.reply(event, "需要登录后使用，请先 #kg登录")
        event.stop_event()
        return
    try:
        pls = await kgapi.user_playlist(uid)
        if not pls:
            await service.reply(event, "暂无歌单")
            event.stop_event()
            return
        data = cardlib.build_playlist_card_data("我的歌单", pls, subtitle="我创建/收藏的歌单", cfg=service.cfg())
        await service.reply_card_or_text(
            event,
            tpl_name="kg-playlist",
            data=data,
            format_text=lambda d: cardlib.format_playlist_text("我的歌单", pls),
        )
    except ApiError as err:
        service.log_warn(f"我的歌单失败: {err}")
        await service.reply(event, f"获取歌单失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def recent_song(service: MusicService, event: AstrMessageEvent):
    """#kg最近：最近播放歌曲（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    try:
        songs = await kgapi.user_listen(pagesize=30)
        if not songs:
            await service.reply(event, "暂无最近播放记录")
            event.stop_event()
            return
        await service.list_to_session(event, "最近播放", songs[:20])
    except ApiError as err:
        service.log_warn(f"最近播放失败: {err}")
        await service.reply(event, f"获取最近播放失败：{err}\n需要先 #kg登录")
    event.stop_event()


async def user_history(service: MusicService, event: AstrMessageEvent):
    """#kg听歌排行：听歌排行（需登录）"""
    if not service.cfg().get("enable", True):
        return
    if not await service.require_login(event):
        return
    try:
        songs = await kgapi.user_history(pagesize=30)
        if not songs:
            await service.reply(event, "暂无听歌排行记录")
            event.stop_event()
            return
        await service.list_to_session(event, "听歌排行", songs[:20])
    except ApiError as err:
        service.log_warn(f"听歌排行失败: {err}")
        await service.reply(event, f"获取听歌排行失败：{err}\n需要先 #kg登录")
    event.stop_event()


ROUTES = [
    Route(
        pattern=re.compile(r"^#?(?:kg登录|kg扫码登录|酷狗登录|酷狗扫码登录)$", re.IGNORECASE),
        name="start_qr_login",
        doc="#kg登录：扫码登录酷狗账号",
        run=start_qr_login,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kgqq登录|kgqq扫码登录|酷狗qq登录|酷狗qq扫码登录)$", re.IGNORECASE),
        name="start_qq_qr_login",
        doc="#kgqq登录：通过 QQ 扫码登录绑定酷狗账号",
        run=start_qq_qr_login,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg状态|kg登录状态|kgs)$", re.IGNORECASE),
        name="login_status_cmd",
        doc="#kg状态 / #kgs：查看登录状态",
        run=login_status_cmd,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg登出|kg注销|kg解绑)$", re.IGNORECASE),
        name="logout",
        doc="#kg登出：清除酷狗登录 Cookie",
        run=logout,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*云盘$", re.IGNORECASE),
        name="cloud",
        doc="#kg云盘：我的云盘歌曲（需登录）",
        run=cloud,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*已购$", re.IGNORECASE),
        name="purchased",
        doc="#kg已购：已购单曲/专辑（需登录）",
        run=purchased,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*等级$", re.IGNORECASE),
        name="grade",
        doc="#kg等级：听歌等级（需登录）",
        run=grade,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*(关注|取关|取消关注)\s+(.+)$", re.IGNORECASE),
        name="follow_toggle",
        doc="#kg关注 歌手 / #kg取关 歌手：关注/取关歌手（需登录）",
        run=follow_toggle,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*关注新歌$", re.IGNORECASE),
        name="follow_newsongs",
        doc="#kg关注新歌：关注的歌手新歌（需登录）",
        run=follow_newsongs,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*关注列表$", re.IGNORECASE),
        name="follow_list",
        doc="#kg关注列表：我关注的歌手（需登录）",
        run=follow_list,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*我的歌单$", re.IGNORECASE),
        name="my_playlist",
        doc="#kg我的歌单：我创建/收藏的歌单（需登录）",
        run=my_playlist,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*最近$", re.IGNORECASE),
        name="recent_song",
        doc="#kg最近：最近播放歌曲（需登录）",
        run=recent_song,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*听歌排行$", re.IGNORECASE),
        name="user_history",
        doc="#kg听歌排行：听歌排行（需登录）",
        run=user_history,
        admin=True,
        priority=6,
    ),
]
