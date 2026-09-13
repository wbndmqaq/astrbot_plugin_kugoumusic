from __future__ import annotations

import asyncio
import os
import re
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from ..core.service import MusicService

from ..core import api as kgapi
from ..core import cards as cardlib
from ..core import panels
from ..core.api import ApiError
from ..core.messages import CONFIG_SAVE_FAILED
from ..core.quality import QUALITY_LABEL
from ..core.service import PLUGIN_DIR
from .base import Route

# metadata.yaml 在进程内不会变，读一次即缓存（原先每次 #kg帮助 都同步读盘 + 解析 YAML）
_VERSION_CACHE: str | None = None


async def _plugin_version() -> str:
    """读取插件版本号（进程内缓存，读盘走线程池，不在事件循环里同步 IO）。"""
    global _VERSION_CACHE
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE

    def _read() -> str:
        try:
            import yaml

            with open(
                os.path.join(PLUGIN_DIR, "metadata.yaml"), "r", encoding="utf-8"
            ) as f:
                meta = yaml.safe_load(f) or {}
            return str(meta.get("version", "?")).lstrip("v")
        except Exception:
            return "?"

    _VERSION_CACHE = await asyncio.to_thread(_read)
    return _VERSION_CACHE


def _route_count() -> str:
    """真实注册的路由条数（延迟导入避免与 handlers/__init__ 形成循环导入）。"""
    from . import ALL_ROUTES
    return str(len(ALL_ROUTES))


async def hot_search(service: MusicService, event: AstrMessageEvent):
    """#kg热搜：酷狗热搜榜"""
    if not service.cfg().get("enable", True):
        return
    try:
        items = await kgapi.hot_search()
        if not items:
            await service.reply(event, "暂无热搜数据")
            event.stop_event()
            return
        data = cardlib.build_hot_card_data(items)
        await service.reply_card_or_text(
            event, tpl_name="kg-hot", data=data, format_text=lambda d: cardlib.format_hot_text(items)
        )
    except ApiError as err:
        service.log_warn(f"热搜失败: {err}")
        await service.reply(event, f"获取热搜失败：{err}")
    event.stop_event()


async def help_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg帮助：查看全部指令"""
    if not service.cfg().get("enable", True):
        return
    try:
        version = await _plugin_version()
        data = cardlib.build_help_card_data(version, service.cfg(), stat_commands=_route_count())
        await service.reply_card_or_text(
            event,
            tpl_name="kg-help",
            data=data,
            format_text=lambda d: cardlib.format_help_text(service.cfg(), version),
        )
    except Exception as err:
        service.log_warn(f"帮助失败: {err}")
        await service.reply(event, cardlib.format_help_text(service.cfg()))
    event.stop_event()


async def settings(service: MusicService, event: AstrMessageEvent):
    """#kg设置：查看插件设置"""
    # 面板数据（含 uid 读取的异常兜底）由 core/panels.py 统一构造
    cfg, uid, data = await panels.build_settings_panel(service)
    await service.reply_card_or_text(
        event,
        tpl_name="kg-settings",
        data=data,
        format_text=lambda d: cardlib.format_settings_text(cfg, uid),
    )
    event.stop_event()


async def quality_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg音质 <档位>：修改音质（auto/viper_tape/viper_clear/super/high/flac/320/128）"""
    m = re.match(r"^#?(?:kg|KG)\s*音质\s*(.+)$", event.message_str.strip(), re.IGNORECASE)
    q = (m.group(1).strip().lower() if m else "").strip()
    if q not in QUALITY_LABEL:
        await service.reply(event, f"音质档位无效。可选：{' / '.join(QUALITY_LABEL.keys())}")
        event.stop_event()
        return
    service.plugin.config["quality"] = q
    if not await service.save_config():
        await service.reply(
            event,
            CONFIG_SAVE_FAILED,
        )
        event.stop_event()
        return
    await service.reply(event, f"已设置音质：{QUALITY_LABEL.get(q, q)}")
    event.stop_event()


async def api_cmd(service: MusicService, event: AstrMessageEvent):
    """#kg api <地址>：修改 API 地址"""
    m = re.match(r"^#?(?:kg|KG)\s*api\s*(https?://\S+)$", event.message_str.strip(), re.IGNORECASE)
    url = m.group(1).strip().rstrip("/") if m else ""
    service.plugin.config["apiBase"] = url
    if not await service.save_config():
        await service.reply(
            event,
            CONFIG_SAVE_FAILED,
        )
        event.stop_event()
        return
    await service.reply(event, f"已设置 API 地址：{url}")
    event.stop_event()


async def api_test(service: MusicService, event: AstrMessageEvent):
    """#kg测试：测试 API 连通"""
    cfg = service.cfg()
    base = str(cfg.get("apiBase") or "")
    if not base:
        await service.reply(event, "⚠ API 未配置，请使用 #kg api <地址> 配置")
        event.stop_event()
        return
    try:
        lst = await kgapi.search("测试", pagesize=1) or []
        masked = cardlib.mask_api_base(base)
        await service.reply(event, f"✅ API 连通正常：{masked}\n搜索结果 {len(lst)} 条")
    except ApiError as e:
        await service.reply(event, f"❌ API 连接失败：{e}")
    event.stop_event()


ROUTES = [
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*热搜$", re.IGNORECASE),
        name="hot_search",
        doc="#kg热搜：酷狗热搜榜",
        run=hot_search,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*(help|帮助|菜单)$", re.IGNORECASE),
        name="help",
        doc="#kg帮助：查看全部指令",
        run=help_cmd,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg设置|kg配置|酷狗设置)$", re.IGNORECASE),
        name="settings",
        doc="#kg设置：查看插件设置",
        run=settings,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*音质\s*(.+)$", re.IGNORECASE),
        name="quality_cmd",
        doc="#kg音质 <档位>：修改音质（auto/viper_tape/viper_clear/super/high/flac/320/128）",
        run=quality_cmd,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg|KG)\s*api\s*(https?://\S+)$", re.IGNORECASE),
        name="api_cmd",
        doc="#kg api <地址>：修改 API 地址",
        run=api_cmd,
        admin=True,
    ),
    Route(
        pattern=re.compile(r"^#?(?:kg测试|酷狗测试)$", re.IGNORECASE),
        name="api_test",
        doc="#kg测试：测试 API 连通",
        run=api_test,
        admin=True,
        priority=6,
    ),
]
