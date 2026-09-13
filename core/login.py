"""扫码登录：二维码轮询骨架、登录落盘与 token 刷新。

原先 ``MusicService`` 里有两份逐行复制的轮询实现（酷狗 App 扫码 ``start_poll``
与 QQ 扫码 ``start_qq_poll``，各含一个约 40 行的内嵌 ``_tick``）。本模块把它们
合并成**一个**轮询骨架 :func:`_tick`，差异（取状态 / 判成功 / 成功回调 / 过期文案
/ 失败文案 / 时限）通过 :class:`PollSpec` 参数化传入。

``MusicService`` 上 ``start_poll`` / ``start_qq_poll`` / ``stop_poll`` /
``refresh_token_once`` 的名字与签名保持不变，改为对本模块的薄委托（``finish_login``
只由本模块的轮询骨架经 ``PollSpec.on_success`` 调用，``MusicService`` 上的死委托已删）；
``active_logins`` 仍由 ``MusicService`` 持有并作为唯一状态容器传入。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from .service import MusicService

from . import api as kgapi
from .api import ApiError

# 扫码登录轮询间隔（秒）
POLL_INTERVAL_SEC = 2.0
# 轮询忙碌时的重排间隔（秒）
POLL_BUSY_INTERVAL_SEC = 0.8

# 轮询动作：继续等待 / 已扫码提示 / 授权成功 / 二维码失效
ACTION_CONTINUE = "continue"
ACTION_SCANNED = "scanned"
ACTION_SUCCESS = "success"
ACTION_EXPIRED = "expired"


@dataclass(frozen=True)
class PollSpec:
    """一次扫码登录轮询的差异化描述（取状态、判成功、成功回调、文案、时限）。"""

    probe: Callable[[dict], Awaitable[dict]]
    classify: Callable[[dict, dict], tuple[str, str]]
    on_success: Callable[..., Awaitable[None]]
    expired_msg: str
    fail_streak_msg: str
    fail_exhausted_msg: str
    max_sec: int


# ──────────── 轮询骨架 ────────────


async def _tick(
    svc: MusicService,
    event: AstrMessageEvent,
    task: dict,
    user_key: str,
    spec: PollSpec,
    state: dict,
    schedule: Callable[[float], Any],
) -> None:
    """单次轮询：与重构前的两个内嵌 ``_tick`` 逐步等价（不改变分支顺序）。"""
    if task["stopped"]:
        return
    if task["busy"]:
        schedule(POLL_BUSY_INTERVAL_SEC)
        return
    if time.time() - state["started"] > spec.max_sec:
        task["stopped"] = True
        svc.active_logins.pop(user_key, None)
        await svc.reply(event, spec.expired_msg)
        return
    task["busy"] = True
    try:
        info = await spec.probe(task)
        action, message = spec.classify(task, info)
        if action == ACTION_EXPIRED:
            task["stopped"] = True
            svc.active_logins.pop(user_key, None)
            await svc.reply(event, message)
            return
        if action == ACTION_SCANNED:
            task["notifiedScan"] = True
            await svc.reply(event, message)
        elif action == ACTION_SUCCESS:
            await spec.on_success(svc, event, info, user_key, task)
            return
        task["failStreak"] = 0
    except Exception as err:  # noqa: BLE001 单次轮询失败不影响后续重试
        task["failStreak"] += 1
        if task["failStreak"] == 5:
            await svc.reply(event, spec.fail_streak_msg.format(err=err))
        if task["failStreak"] >= 25:
            task["stopped"] = True
            svc.active_logins.pop(user_key, None)
            await svc.reply(event, spec.fail_exhausted_msg)
            return
    finally:
        task["busy"] = False
    if not task["stopped"] and svc.active_logins.get(user_key, {}).get("key") == task["key"]:
        task["timer"] = schedule(POLL_INTERVAL_SEC)


def _launch_poll(svc: MusicService, event: AstrMessageEvent, task: dict, user_key: str, spec: PollSpec) -> None:
    """启动轮询：登记调度器并排下首跳（定时器句柄与轮询任务都留在 ``task['jobs']``）。"""
    state = {"started": time.time()}
    loop = asyncio.get_running_loop()

    def _spawn():
        t = asyncio.create_task(_tick(svc, event, task, user_key, spec, state, _schedule))
        task["jobs"].append(t)
        # 修剪已完成的旧任务引用：每跳只保留最近 8 个条目，避免长轮询里
        # jobs 无界增长（每 tick 累积一个 task + 一个 timer 句柄）。
        if len(task["jobs"]) > 8:
            del task["jobs"][:-8]
        return t

    def _schedule(delay: float):
        handle = loop.call_later(delay, _spawn)
        task["jobs"].append(handle)
        return handle

    task["timer"] = _schedule(POLL_INTERVAL_SEC)


def stop_poll(svc: MusicService, user_key: str) -> None:
    """停止某用户的登录轮询（登出/重载/重新登录前调用）。"""
    task = svc.active_logins.pop(user_key, None)
    if not task:
        return
    task["stopped"] = True
    if task.get("timer") is not None:
        try:
            task["timer"].cancel()
        except Exception:  # noqa: BLE001
            pass
    # 取消所有已创建但仍在运行的 _tick 轮询任务，避免重载/登出后残留协程对旧 event 发消息
    for job in task.get("jobs") or []:
        if job and hasattr(job, "cancel"):
            try:
                job.cancel()
            except Exception:  # noqa: BLE001
                pass


# ──────────── 酷狗 App 扫码 ────────────


def _qr_probe(task: dict) -> Awaitable[dict]:
    return kgapi.qr_check(task["key"])


def _qr_classify(task: dict, info: dict) -> tuple[str, str]:
    status = info.get("status")
    if status == 0:
        return ACTION_EXPIRED, "二维码已失效，请重新 #kg登录"
    if status == 2 and not task["notifiedScan"]:
        return ACTION_SCANNED, "已扫码，请在手机上确认登录"
    if status == 4:
        return ACTION_SUCCESS, ""
    return ACTION_CONTINUE, ""


def _qr_spec(max_sec: int) -> PollSpec:
    return PollSpec(
        probe=_qr_probe,
        classify=_qr_classify,
        on_success=finish_login,
        expired_msg="二维码已过期，请重新 #kg登录",
        fail_streak_msg="轮询暂时失败：{err}（继续重试）",
        fail_exhausted_msg="轮询失败过多，请检查 API 服务或重新 #kg登录",
        max_sec=max_sec,
    )


def start_poll(svc: MusicService, event: AstrMessageEvent, key: str, max_sec: int = 300) -> None:
    """开始轮询酷狗 App 扫码状态（二维码约 5 分钟有效）。"""
    user_key = svc.user_key(event)
    task = {"key": key, "stopped": False, "busy": False, "notifiedScan": False, "failStreak": 0, "jobs": []}
    svc.active_logins[user_key] = task
    _launch_poll(svc, event, task, user_key, _qr_spec(max_sec))


# ──────────── QQ 扫码 ────────────


def _qq_check_params(qr_ctx: dict) -> dict:
    return {
        "qrsig": qr_ctx.get("qrsig") or "",
        "ptqrtoken": qr_ctx.get("ptqrtoken") or "",
        "pt_login_sig": qr_ctx.get("pt_login_sig") or "",
        "pt_openlogin_data": qr_ctx.get("pt_openlogin_data") or "",
        "xlogin_url": qr_ctx.get("xlogin_url") or "",
        "cookie": qr_ctx.get("cookie") or "",
    }


async def _qq_probe(task: dict) -> dict:
    return await kgapi.login_qq_qr_check(_qq_check_params(task["ctx"]))


def _qq_classify(task: dict, info: dict) -> tuple[str, str]:
    status = str(info.get("status") if info.get("status") is not None else "")
    msg = str(info.get("msg") or "")
    if status in ("expired", "65"):
        return ACTION_EXPIRED, "QQ 二维码已失效，请重新 #kgqq登录"
    if (status in ("wait", "66") or "扫码" in msg) and not task["notifiedScan"]:
        if "确认" in msg:
            return ACTION_SCANNED, "已扫码，请在手机 QQ 上确认授权登录"
        return ACTION_CONTINUE, ""
    if status in ("0", "1") or info.get("token"):
        # 授权成功换取了 token
        return ACTION_SUCCESS, ""
    return ACTION_CONTINUE, ""


def _qq_spec(max_sec: int) -> PollSpec:
    return PollSpec(
        probe=_qq_probe,
        classify=_qq_classify,
        on_success=finish_login,
        expired_msg="QQ 二维码已过期，请重新 #kgqq登录",
        fail_streak_msg="QQ 轮询暂时失败：{err}（继续重试）",
        fail_exhausted_msg="QQ 轮询失败过多，请检查 API 服务或重新 #kgqq登录",
        max_sec=max_sec,
    )


def start_qq_poll(svc: MusicService, event: AstrMessageEvent, qr_ctx: dict, max_sec: int = 180) -> None:
    """开始轮询 QQ 扫码状态（二维码约 2 分钟有效）。"""
    user_key = svc.user_key(event)
    qrsig = qr_ctx.get("qrsig") or ""
    task = {
        "key": f"qq_{qrsig}",
        "ctx": qr_ctx,
        "stopped": False,
        "busy": False,
        "notifiedScan": False,
        "failStreak": 0,
        "jobs": [],
    }
    svc.active_logins[user_key] = task
    _launch_poll(svc, event, task, user_key, _qq_spec(max_sec))


# ──────────── 登录落盘与 token 刷新 ────────────


async def finish_login(
    svc: MusicService, event: AstrMessageEvent, info: dict, user_key: str, task: dict
) -> None:
    """登录成功：写入插件配置并回发状态卡（写盘失败如实告知）。"""
    task["stopped"] = True
    svc.active_logins.pop(user_key, None)
    token = info.get("token") or ""
    userid = info.get("userid") or ""
    nickname = info.get("nickname") or ""
    if not token or not userid:
        await svc.reply(event, "登录成功但未获取到 Cookie（可能登录状态异常），请重新 #kg登录")
        return
    cookie = f"token={token};userid={userid}"
    svc.plugin.config["defaultCookie"] = cookie
    svc.plugin.config["defaultUid"] = str(userid)
    ok = await svc.save_config()
    if ok:
        svc.log_info("扫码登录成功，Cookie 已写入插件配置 defaultCookie")
        await svc.reply(
            event, f"✅ 登录成功：{nickname or userid or '已写入 Cookie'}\nCookie 已存入插件配置，全群默认使用该账号"
        )
    else:
        # 保存失败必须如实告知：内存中已生效（本次会话可用），但重启/重载后会丢失
        svc.log_warn("扫码登录成功，但 Cookie 未能写入插件配置")
        await svc.reply(
            event,
            f"⚠ 登录成功：{nickname or userid or '已获取 Cookie'}，但 Cookie 未能写入插件配置"
            "（本机 AstrBot 版本可能过旧），重启后会失效；"
            "请到 WebUI 插件配置中手动填写 defaultCookie，或升级 AstrBot",
        )
    # 刷新登录 token 延长过期时间（fire-and-forget）
    svc._spawn_bg(svc.refresh_token_once())
    await svc.send_status(event, status_data=None)


async def refresh_token_once(svc: MusicService) -> None:
    try:
        await kgapi.login_token_refresh()
        svc.log_info("已刷新酷狗登录 token")
    except ApiError as e:
        svc.log_warn(f"刷新登录 token 失败: {e}")
