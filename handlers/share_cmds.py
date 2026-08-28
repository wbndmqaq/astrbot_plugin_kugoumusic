from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent, filter

if TYPE_CHECKING:
    from ..core.service import MusicService

try:
    from ..core.service import collect_message_text, is_kg_message, is_plugin_command_msg
except ImportError:
    from core.service import collect_message_text, is_kg_message, is_plugin_command_msg
from .base import Route


async def resolve(service: MusicService, event: AstrMessageEvent):
    """酷狗音乐分享卡片/链接自动解析。

    用全量事件而非 @filter.regex：OneBot json 段（分享卡片）不会写入
    message_str，regex 过滤器永远匹配不到 → 群里发卡片无反应。
    """
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableResolve") is False:
        return
    msg_str = str(event.message_str or "")
    chain = getattr(getattr(event, "message_obj", None), "message", None) or []
    has_json = any(type(seg).__name__ == "Json" for seg in chain)
    if not has_json and not is_kg_message(msg_str):
        return
    text = collect_message_text(event)
    if not is_kg_message(text):
        return
    if is_plugin_command_msg(msg_str):
        return
    handled = await service.handle_resolve(event, text)
    if handled:
        event.stop_event()


ROUTES = [
    Route(
        pattern=None,
        name="resolve",
        doc="酷狗音乐分享卡片/链接自动解析",
        run=resolve,
        priority=5,
        event_message_type=filter.EventMessageType.ALL,
    ),
]
