"""astrbot_plugin_kugoumusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any

import aiohttp

# 单次 API 请求超时（秒）
API_TIMEOUT_SEC = 20
# 酷狗 API 错误码（KuGouMusicApi server 把上游失败统一转成 HTTP 502，
# body 携带 error_code；业务层面 status==1 成功、status==2 音质/付费降级信号）
ERR_MESSAGES = {
    152: "搜索需要登录（152）：酷狗已禁止匿名搜索，请发送 #kg登录 扫码登录后使用",
    20010: "需要登录（20010）：请发送 #kg登录 扫码登录后使用",
    20017: "需要登录或 Token 失效（20017）：请发送 #kg登录",
    20018: "需要登录或 Token 失效（20018）：请发送 #kg登录",
    20028: "请求触发风控（20028）：请求过快或设备 Cookie 异常，冷却后重试",
    20040: "设备 Cookie 异常（20040）：请重启插件重新注册设备",
}
def _upstream_msg(data: dict) -> str:
    """取上游错误文案。

    KuGouMusicApi 的失败响应体是 ``{status: 0, msg: "..."}``（见各 module 的
    ``answer.body = { status: 0, msg: e }``），少数接口用 ``error_msg``。
    原先的候选链缺 ``msg``，导致所有上游失败都被降级成通用「HTTP 502」文案，
    丢失「缺少 qrsig（请先调用 /login/qq/qr/create）」这类关键定位信息。
    """
    return str(
        data.get("msg")
        or data.get("error_msg")
        or data.get("errmsg")
        or data.get("message")
        or data.get("info")
        or ""
    )
class ApiError(Exception):
    """酷狗 API 调用失败。

    ``timeout=True`` 标记「本次失败是**总超时**」这一类错误：调用方在音质阶梯 /
    多档回退循环里必须对超时**立即上抛**，不能当成「该档不可用」继续降级——
    每档都超时会让上游尝试次数 = 阶梯长度（实测 8 次 × API_TIMEOUT_SEC=20 秒
    ≈ 160 秒无响应，#kg听所有 串行 30 首时会被放大 30 倍）。
    """

    def __init__(self, message: str, *, code=None, payload=None, timeout: bool = False):
        super().__init__(message)
        self.code = code
        self.payload = payload
        self.timeout = bool(timeout)
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
def safe_int(value, default: int = 0) -> int:
    """把配置/上游取值转成 int：``None``/空串/非数字字符串一律回落 ``default``。

    WebUI 允许把任意字段清空（值为 ``None``）或手填非数字（``"abc"``）；直接
    ``int(cfg.get(...) or X)`` 会抛 ``ValueError``/``TypeError``——该异常不是
    ``ApiError``，handler 接不住 → 用户收到 0 条回复（消息还会继续流向后续管道）。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
def cfg_int(cfg: dict, key: str, default: int = 0) -> int:
    """读整数型插件配置（``safe_int`` 的配置版：多一层 cfg 取值兜底）。"""
    try:
        return safe_int((cfg or {}).get(key), default)
    except Exception:
        return default
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
# ──────────── 复用 HTTP 会话 ────────────

# 模块级 aiohttp.ClientSession 复用：避免每请求新建会话（反复 TCP 握手 + DNS 解析）。
# 会话由 service.terminate() 调 close_session() 统一关闭；懒加载，未使用时不会创建。
_session: aiohttp.ClientSession | None = None
def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session
def get_session() -> aiohttp.ClientSession:
    """公开访问器：供 delivery 等模块复用同一 ClientSession（语义同 _get_session）。"""
    return _get_session()
async def close_session() -> None:
    """关闭并释放复用的 HTTP 会话（插件卸载/重载时由 service 调用）。"""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
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
    # 非有限值（NaN/±inf）一律归零：上游 json 默认接受 -Infinity，漏过守卫会让
    # 下游 int(_num(...)) 抛 OverflowError（且不是 ApiError，handler 接不住）
    return f if math.isfinite(f) else 0
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
    *,
    inject_cookie: bool = True,
    anon: bool = False,
) -> dict:
    """请求 KuGouMusicApi，返回业务 body dict（已做状态/错误码校验）。

    anon=True 时未登录会补匿名占位 token/userid（仅 /search 类需要）。
    """
    params = dict(params or {})
    base = _get_base()
    if not base:
        raise ApiError("API 地址未配置：请发送 #kg api <地址>，或在插件设置面板填写 apiBase")
    if "://" not in base:
        raise ApiError(f"API 地址格式错误（缺少 http:// 协议头）：{base}")
    url = f"{base}{pathname if pathname.startswith('/') else '/' + pathname}"
    if inject_cookie:
        # 必须与调用方传入的 cookie **合并**而不是覆盖：/login/qq/qr/check 要求把
        # qr/create 返回的会话 cookie（含 qrsig）原样带回，覆盖会让该接口必然 502
        # （上游会明确报「缺少 cookie（请先调用 /login/qq/qr/create…）」）。
        caller_cookie = str(params.pop("cookie", "") or "")
        base_cookie = _compose_cookie_str(anon=anon)
        params["cookie"] = ";".join(p for p in (base_cookie, caller_cookie) if p)

    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SEC)
    try:
        sess = _get_session()
        if method == "get":
            async with sess.get(url, params=_query_safe_params(params), timeout=timeout) as res:
                return await _handle_response(res, pathname)
        else:
            # POST 体同样带上 cookie（服务端 query/body 的 cookie 都会被解析合并）
            async with sess.post(url, data=_query_safe_params(params), timeout=timeout) as res:
                return await _handle_response(res, pathname)
    except aiohttp.ClientConnectorError as e:
        raise ApiError(f"无法连接酷狗 API（{base}），请确认 KuGouMusicApi 服务已启动") from e
    except aiohttp.ServerTimeoutError as e:
        raise ApiError(f"请求超时：{base}", timeout=True) from e
    except aiohttp.ClientError as e:
        raise ApiError(f"网络错误：{e}") from e
    except (TimeoutError, asyncio.TimeoutError) as e:
        # ClientTimeout(total=...) 到期抛的是 asyncio.TimeoutError（Python 3.11 起即内建
        # TimeoutError，MRO 为 TimeoutError → OSError），**不是** aiohttp.ClientError，
        # 只写 ClientError 会让总超时穿透到 handler：except ApiError 接不住 → 用户拿不到
        # 任何错误回复，且 event.stop_event() 不执行（消息继续流向后续管道/LLM）。
        # 上面的 ServerTimeoutError 分支只管读写类超时，总超时必须在 ClientError 之后补捕获。
        # timeout=True：让 song_url_best 之类的阶梯循环能识别「总超时」并立即上抛。
        raise ApiError(f"请求超时：{base}", timeout=True) from e
async def _handle_response(res: aiohttp.ClientResponse, pathname: str = "") -> dict:
    status = res.status
    try:
        data = await res.json(content_type=None)
    except Exception:
        text = (await res.text())[:200]
        raise ApiError(f"返回非 JSON（HTTP {status}，{pathname or '?'}）：{text}")
    if not isinstance(data, dict):
        raise ApiError(f"返回格式异常（HTTP {status}）")
    if status >= 400:
        code = data.get("error_code") or data.get("errcode") or data.get("err_code")
        msg = _upstream_msg(data)
        raise ApiError(str(msg) or _err_msg_for(code, status), code=code, payload=data)
    # 业务失败：status==0（服务端会转成 502，此处双保险）
    if data.get("status") == 0:
        code = data.get("error_code") or data.get("errcode") or data.get("err_code")
        msg = _upstream_msg(data)
        raise ApiError(str(msg) or _err_msg_for(code), code=code, payload=data)
    return data
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
