"""状态卡 / 设置卡：数据构造与发送。

本模块收拢两组面板职责，来源不同：
- ``build_status`` / ``send_status``：从 ``MusicService`` 搬出；``MusicService`` 仅保留
  ``send_status`` 同名签名并薄委托到本模块（``build_status`` 在 MusicService 上零引用，
  已删除该死委托，调用方直接用本模块函数）。
- ``build_settings_panel``：从 ``handlers/system_cmds.settings()`` 抽出的新函数（数据的
  取用与异常兜底原先内联在该 handler 里），并非从 ``MusicService`` 搬出。

本模块只负责「取数 → 交给 cards 构造卡片数据 / 文本 → 发送」，不持有状态。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from .service import MusicService

from . import api as kgapi
from . import cards as cardlib
from .api import ApiError


async def build_status(svc: MusicService, *, status_data: dict | None = None) -> dict:
    """汇总登录状态（配置 / 用户详情）；``status_data`` 非空表示「刚登录成功」直取。"""
    cfg = svc.cfg()
    default_cookie = str(cfg.get("defaultCookie") or "")
    status = {
        "loggedIn": False,
        "nickname": "",
        "avatar": "",
        "uin": "",
        "level": "",
        "vipLabel": "",
        "apiBase": cfg.get("apiBase") or "",
        "keyStatus": "默认 Cookie" if default_cookie else "无 Cookie",
        "quality": str(cfg.get("quality") or "auto"),
    }
    if status_data is not None:
        # 登录刚成功：直接从登录信息构建
        status["loggedIn"] = True
        status["nickname"] = status_data.get("nickname") or ""
        status["uin"] = str(status_data.get("userid") or "")
        return status
    if not default_cookie:
        return status
    uid = await svc.get_uid()
    if not uid:
        status["keyStatus"] = "Cookie 未含 userid，无法校验"
        return status
    try:
        info = await kgapi.user_detail(uid)
        if info and info.get("name"):
            status["loggedIn"] = True
            status["nickname"] = info.get("name") or ""
            status["avatar"] = info.get("avatar") or ""
            status["uin"] = str(info.get("id") or uid)
            if info.get("level"):
                status["level"] = str(info["level"])
            if info.get("vip"):
                status["vipLabel"] = "酷狗 VIP"
        else:
            status["keyStatus"] = "Cookie 可能已失效"
    except ApiError as e:
        status["keyStatus"] = f"查询失败：{e}"
    return status


async def send_status(svc: MusicService, event: AstrMessageEvent, *, status_data: dict | None = None) -> None:
    """发送状态卡；任何异常都退化为纯文本状态，不让指令静默。"""
    try:
        status = await build_status(svc, status_data=status_data)
        data = cardlib.build_status_card_data(status)
        await svc.reply_card_or_text(
            event, tpl_name="kg-status", data=data, format_text=lambda d: cardlib.format_status_text(status)
        )
    except Exception as err:  # noqa: BLE001 兜底：状态卡失败也要给用户一个可见回复
        svc.log_warn(f"状态卡片失败: {err}")
        await svc.reply(
            event,
            cardlib.format_status_text(
                {
                    "loggedIn": False,
                    "apiBase": svc.cfg().get("apiBase") or "",
                    "quality": str(svc.cfg().get("quality") or "auto"),
                    "keyStatus": str(err),
                }
            ),
        )


async def build_settings_panel(svc: MusicService) -> tuple[dict, str, dict]:
    """``#kg设置`` 面板数据：返回 (配置, uid, 卡片数据)，文本兜底复用同一份配置/uid。"""
    cfg = svc.cfg()
    try:
        uid = await svc.get_uid()
    except Exception:  # noqa: BLE001
        uid = ""
    return cfg, uid, cardlib.build_settings_card_data(cfg, uid)
