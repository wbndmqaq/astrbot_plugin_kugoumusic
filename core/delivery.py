from __future__ import annotations

import asyncio
import os
import re
import shutil
import time

import aiohttp
from astrbot.api import logger
from astrbot.api.message_components import File, Record

from .api import cfg_int, get_session, safe_int
from .quality import trial_label

PLUGIN_NAME = "astrbot_plugin_kugoumusic"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# QQ 官方平台的文件体积限制：超过即跳过文件上传（平台限制语义）
QQ_FILE_SIZE_LIMIT = 10 * 1024 * 1024
# aiocqhttp 压缩触发阈值：超过即先 ffmpeg 压成紧凑 mp3 控制载荷（压缩触发语义）
AIOCQ_COMPRESS_THRESHOLD = 8 * 1024 * 1024
# 流式下载块大小
DOWNLOAD_CHUNK_SIZE = 256 * 1024
# 流式下载落盘攒批大小（攒满一次写盘，避免小块频繁 IO）
DOWNLOAD_FLUSH_SIZE = 1024 * 1024
# 有效音频的最小体积：小于此值视为无效链接的报错页
MIN_AUDIO_BYTES = 256
# 临时文件清理延迟下限（秒）：event.send() 返回后写入方可能仍在读盘，keepFileSec=0 也不能立即删
MIN_CLEANUP_DELAY_SEC = 5
# ``cancel_cleanups()`` 对「刚登记」条目的宽限期（秒）：登记后不足该秒数的临时文件可能
# 正处于「已登记、但发送方仍在读盘」的窗口内（卡片图在 render_card 返回前就登记、
# 调用方随后才 send_chain(Image.fromFileSystem)），此时插件重载/卸载直接补删会误删
# 正在发送的文件。故 cancel 时跳过登记时间不足本值的条目（见 cancel_cleanups）。
CLEANUP_CANCEL_GRACE_SEC = 5

# 临时文件清理登记表 {定时器句柄: (待删路径, 登记时间)}：保存以便插件卸载时取消**并补删**残留文件。
# 模块级共享（含 MusicService.schedule_unlink 的卡片图/二维码，见 core/service.py），
# 不再另设实例级注册表，确保 terminate 一次调用即可清空全部排期。
_cleanup_timers: dict[asyncio.TimerHandle, tuple[str, float]] = {}


def _platform_name(event) -> str:
    try:
        name = event.get_platform_name()
        if not name:
            return ""
        return str(name)
    except Exception:
        return ""


def _is_qqofficial(event) -> bool:
    return "qq_official" in _platform_name(event)


def _is_weixin_oc(event) -> bool:
    return "weixin_oc" in _platform_name(event)


def _is_aiocqhttp(event) -> bool:
    return "aiocqhttp" in _platform_name(event)


def _file_to_base64(path: str) -> str:
    import base64

    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


async def _aiocq_call_action(event, action: str, sid: int, segs: list) -> None:
    """aiocqhttp 直连 OneBot call_action 发送原始消息段。

    bot 实例来自 event.bot（CQHttp）；napcat 与 AstrBot 跨容器时 file:// 路径
    不可见（realpath ENOENT），且 File/Record 组件对 base64:// 有各自的坑
    （File 擦空不存在路径、Record 强制转 WAV），因此走底层 action 直发 base64。
    """
    bot = getattr(event, "bot", None)
    if bot is None:
        raise RuntimeError("无法获取 aiocqhttp bot 实例")
    is_group = action == "send_group_msg"
    if is_group:
        await bot.call_action(action, group_id=int(sid), message=segs)
    else:
        await bot.call_action(action, user_id=int(sid), message=segs)


async def _aiocq_send_file(event, text: str, display: str, path: str) -> None:
    """aiocqhttp 发送文件：无损/大文件已由调用方压成 mp3，这里 base64 内联直发。"""
    b64 = await asyncio.to_thread(_file_to_base64, path)
    segs: list = []
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    segs.append({"type": "file", "data": {"file": f"base64://{b64}", "name": display}})
    is_group = bool(getattr(event.message_obj, "group_id", None))
    sid = event.message_obj.group_id if is_group else event.get_sender_id()
    await _aiocq_call_action(event, "send_group_msg" if is_group else "send_private_msg", int(sid), segs)


async def _aiocq_send_record(event, text: str, src_path: str) -> tuple[bool, str]:
    """aiocqhttp 语音：紧凑音频以 base64:// record 段直发（OneBot v11 标准段）。

    不在插件侧预编码 silk：协议端（NapCat / SnowLuma 等按 OneBot v11 实现的 NTQQ
    框架）对 record 段统一「非 silk → 自带 ffmpeg addon 转 Tencent silk
    （\\x02#!SILK_V3、24kHz 单声道），时长用 ffmpeg 对原始文件精确探测」。
    此前自编的裸 silk 会被协议端按魔数透传——非标码流导致手机无法播放、
    时长探测失败钳到 1 秒。走 base64 直发而非 Record 组件，是为了绕开 AstrBot
    把语音强制转 WAV 再编码（载荷 ~50MB+ → WS 超时）。任何一步失败返回
    (False, 原因)，由调用方退回标准 Record 组件发送。
    """
    try:
        b64 = await asyncio.to_thread(_file_to_base64, src_path)
        segs: list = []
        if text:
            segs.append({"type": "text", "data": {"text": text}})
        segs.append({"type": "record", "data": {"file": f"base64://{b64}"}})
        is_group = bool(getattr(event.message_obj, "group_id", None))
        sid = event.message_obj.group_id if is_group else event.get_sender_id()
        await _aiocq_call_action(event, "send_group_msg" if is_group else "send_private_msg", int(sid), segs)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _qq_official_chunked_upload_supported(astrbot_version: str | None = None) -> bool:
    """AstrBot ≥ 4.27.3 的 QQ 官方适配器对本地大文件自动走分片上传。

    Args:
        astrbot_version: AstrBot 版本串；缺省取运行时的 ``astrbot.__version__``。
    """
    if astrbot_version is None:
        try:
            from astrbot import __version__ as astrbot_version
        except Exception:
            return False
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", str(astrbot_version))
    return bool(m) and tuple(int(x) for x in m.groups()) >= (4, 27, 3)


def _should_block_qqofficial_file(
    *,
    is_qqoff: bool,
    want_file: bool,
    file_size: int,
    ext: str,
    cfg: dict,
) -> bool:
    """QQ 官方平台是否跳过文件上传，仅发语音。

    开启 ``qqofficialChunkedUpload`` 且 AstrBot ≥ 4.27.3（适配器对本地大文件自动
    分片上传）时放行 FLAC/>10MB 文件；否则旧守卫生效：>10MB 或 .flac 跳过文件上传。
    """
    if not (is_qqoff and want_file):
        return False
    chunk_on = (
        cfg.get("qqofficialChunkedUpload", True) is not False
        and _qq_official_chunked_upload_supported()
    )
    if chunk_on:
        return False
    return file_size > QQ_FILE_SIZE_LIMIT or ext.lower() == ".flac"
# 临时目录：固定在插件数据目录下（``data/plugin_data/<插件名>/temp``）。
# 不放插件自身目录——插件更新/重装会整体替换该目录，会让在途的下载与卡片图失效。
# 首次调用在线程池里建目录（阻塞 IO 不占事件循环），此后直接命中进程内缓存。
_temp_dir: str = ""


async def get_temp_dir() -> str:
    """返回（并按需创建）插件数据目录下的临时目录。"""
    global _temp_dir
    if _temp_dir:
        return _temp_dir

    def _resolve() -> str:
        from astrbot.api.star import StarTools

        d = StarTools.get_data_dir(PLUGIN_NAME) / "temp"
        d.mkdir(parents=True, exist_ok=True)
        # 首次触达时顺手扫地：删掉 1 小时前的残留文件（崩溃/强杀时定时清理与
        # terminate 补删都不会执行，孤儿文件只能在这里回收）。在途文件几分钟内
        # 就会被正常清理，不会被误删。
        try:
            import time as _time

            now = _time.time()
            # 固定 1 小时阈值：本函数拿不到插件配置（tempDir 已固定到 plugin_data），
            # 而 keepFileSec 的常规清理定时器本就会在保留期后删除文件，1 小时兜底
            # 只回收「崩溃/强杀导致定时器丢失」的孤儿，不会与正常清理竞争
            for f in d.iterdir():
                if not f.is_file():
                    continue
                try:
                    if now - f.stat().st_mtime > 3600:
                        f.unlink(missing_ok=True)
                except OSError:
                    continue  # 单文件 stat/unlink 竞态不中断整个扫地
        except Exception:  # noqa: BLE001  扫地失败不影响本次使用
            pass
        return str(d)

    _temp_dir = await asyncio.to_thread(_resolve)
    return _temp_dir




def _clean_track_text(s: str, max_len: int = 40) -> str:
    if not s:
        return ""
    s = str(s)
    s = s.replace("【", "(").replace("】", ")").replace("《", "(").replace("》", ")")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len]
    return re.sub(r'[\\/:*?"<>|]', "", s).strip()


def build_music_filename(*, singer: str, title: str, ext: str = "") -> str:
    s = _clean_track_text(singer, 30)
    t = _clean_track_text(title, 40)
    base = f"{s}-{t}" if (s and t) else (s or t or "KugouMusic")
    return f"{base}{ext}"


def _ext_for_quality(quality_hint: str, url: str) -> str:
    q = (quality_hint or "").lower()
    if q in ("flac", "ape", "lossless"):
        return ".flac"
    u = (url or "").lower()
    for ext in (".flac", ".ogg", ".m4a", ".mp3", ".wav", ".ape"):
        if ext in u:
            return ext
    return ".mp3"


async def download_audio(
    url: str, save_dir: str, filename: str = "kugou", timeout_ms: int = 90000, quality_hint: str = ""
) -> dict:
    """流式下载音频到本地临时文件；内容过小/HTML 报错，失败时清理残留文件。"""
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    ext = _ext_for_quality(quality_hint, url)
    safe_name = re.sub(r"[^\w.-]", "", filename) or "kugou"
    file_path = os.path.join(save_dir, f"{safe_name}_{int(time.time() * 1000)}{ext}")

    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    size = 0
    # 确保目标文件不存在（时间戳通常唯一，此处双保险避免 ab 追加到残留文件）；
    # stat/remove 是阻塞 IO，丢线程池执行（与本函数其余落盘路径同口径）
    if await asyncio.to_thread(os.path.exists, file_path):
        try:
            await asyncio.to_thread(os.remove, file_path)
        except Exception:
            pass
    try:
        # 复用 core/api.py 的模块级会话（避免每次下载重建 TCP/DNS 握手）；
        # 超时按「每请求」传入，不改复用会话的全局默认超时。
        sess = get_session()
        async with sess.get(url, headers=headers, timeout=timeout, allow_redirects=True) as res:
            if res.status >= 400:
                raise RuntimeError(f"下载失败 HTTP {res.status}")
            # 流式分块下载：避免整文件读进内存；每攒 ~1MB 落盘一次（线程池，不阻塞事件循环）
            pending = bytearray()
            first_chunk = True
            async for chunk in res.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                if first_chunk:
                    head = chunk[:32].decode("utf-8", errors="ignore").lower()
                    if "<html" in head or "<!doctype" in head:
                        raise RuntimeError("下载内容为 HTML，音频链接已失效")
                    first_chunk = False
                size += len(chunk)
                pending.extend(chunk)
                if len(pending) >= DOWNLOAD_FLUSH_SIZE:
                    await asyncio.to_thread(_append_bytes, file_path, bytes(pending))
                    pending.clear()
            if pending:
                await asyncio.to_thread(_append_bytes, file_path, bytes(pending))
            if size < MIN_AUDIO_BYTES:
                raise RuntimeError("下载内容过小，可能是无效链接")
    except (TimeoutError, asyncio.TimeoutError) as e:
        # 与 core/api.py 同口径：ClientTimeout(total=...) 到期抛的是 asyncio.TimeoutError
        # （非 aiohttp.ClientError / RuntimeError），其 str() 为空串，直接上抛会让用户看到
        # 「下载音频失败：（空）」。转成带时长与原因文案的 RuntimeError，仍是 Exception
        # 子类，deliver_song 的兜底分支照旧能接到；半截文件与其它失败路径一样先删掉。
        _remove_file_quietly(file_path)
        raise RuntimeError(f"下载超时（{timeout_ms / 1000:.0f} 秒）：直链响应过慢，可稍后重试或换一首歌") from e
    except Exception:
        _remove_file_quietly(file_path)
        raise
    return {"filePath": file_path, "size": size}


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)


def _append_bytes(path: str, data: bytes):
    with open(path, "ab") as f:
        f.write(data)


def _schedule_cleanup(file_path: str, keep_sec):
    """延时清理临时文件。

    延迟取 ``max(keep_sec, MIN_CLEANUP_DELAY_SEC)``：``keepFileSec=0`` 也不能立即删
    （``event.send()`` 返回时写入方/协议端可能仍在读盘）。定时器句柄与 ``(路径, 登记时间)``
    一起登记在模块级 ``_cleanup_timers``，插件卸载时由 ``cancel_cleanups()`` 取消**并补删**
    （登记时间用于跳过刚登记、可能仍在发送读盘的条目）。

    ``keep_sec`` 走 ``safe_int``：配置被清空（``None``/空串）或填成非数字时回落 60 秒，
    不会抛 ``TypeError``/``ValueError``（该异常不是 ``ApiError``，handler 接不住）。
    """
    if not file_path:
        return
    delay = max(safe_int(keep_sec, 60), MIN_CLEANUP_DELAY_SEC)
    loop = asyncio.get_running_loop()
    handle: asyncio.TimerHandle | None = None

    def _rm():
        if handle is not None:
            _cleanup_timers.pop(handle, None)
        _remove_file_quietly(file_path)

    handle = loop.call_later(delay, _rm)
    _cleanup_timers[handle] = (file_path, time.time())


def _remove_file_quietly(file_path: str) -> None:
    """best-effort 删除文件（不存在或删除失败都不抛）。"""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass


def cancel_cleanups() -> None:
    """取消待执行的清理定时器，并 best-effort 删除其登记的临时文件。

    只 ``cancel()`` 不删除会让已排期的临时文件**永远**留在 tempDir（回调不再执行，
    插件重载越频繁残留越多），因此取消后统一补删已登记路径。

    例外：登记时间不足 ``CLEANUP_CANCEL_GRACE_SEC`` 秒的条目**既不禁用也不补删**——
    卡片图在 ``render_card`` 返回前就登记、调用方随后才 ``send_chain(Image.fromFileSystem)``，
    压缩语音同理；插件重载/卸载恰好落在这个「已登记、发送尚未读完盘」窗口时，
    补删会把正在发送的文件删掉。选择「按登记时间宽限」而不是「把登记移到发送之后」，
    是因为登记分散在递送 finally（``_schedule_delivery_cleanup``）与卡片渲染两处，
    发送成功/失败/异常路径都要求登记，移到发送之后极易漏登记（回到「永久残留」）。
    未被取消的年轻条目其定时器仍会在原延迟到期时删除文件（不产生新的残留）。

    注册表是模块级的、进程内共享：本进程通常只有一个插件实例，行为与实例级等价；
    多实例时卸载其中一个会提前删掉另一个排期的临时文件——这比「永久残留」可接受，
    故不再保留 ``MusicService._cleanup_timers`` 实例副本（两份注册表各管一半，反而
    会让 terminate 只清掉一半、更容易漏）。
    """
    now = time.time()
    for h, entry in list(_cleanup_timers.items()):
        path, registered_at = entry
        if now - registered_at < CLEANUP_CANCEL_GRACE_SEC:
            continue  # 刚登记：可能仍在发送读盘窗口内，留给定时器按原延迟处理
        try:
            h.cancel()
        except Exception:
            pass
        _cleanup_timers.pop(h, None)
        _remove_file_quietly(path)


async def _send_music_segment(event, music_data: dict) -> bool:
    """以 OneBot v11 ``music`` 段直发原生音乐卡，平台不支持时返回 False。

    适配器实例是 ``event.bot``（CQHttp）；``event.platform`` 只是 PlatformMetadata
    元数据、没有发送接口。OneBot 侧只有 ``call_action``，不存在 ``send_api``。
    """
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if call_action is None:
        return False
    is_group = bool(getattr(event.message_obj, "group_id", None))
    sid = event.message_obj.group_id if is_group else event.get_sender_id()
    target = {"group_id": int(sid)} if is_group else {"user_id": int(sid)}
    action = "send_group_msg" if is_group else "send_private_msg"
    try:
        await call_action(
            action,
            message=[{"type": "music", "data": music_data}],
            **target,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[kugoumusic] 音乐卡片发送失败（{action}）: {e}")
        return False


async def send_native_music_card(event, music_id: str) -> bool:
    """发送 OneBot 原生音乐卡片（type=kugou，仅 aiocqhttp 等 OneBot 协议可用）。"""
    return await _send_music_segment(event, {"type": "kugou", "id": str(music_id)})


# ffmpeg 路径缓存：``shutil.which`` 会遍历 PATH 做多次 stat（阻塞 IO），而它在每首歌
# 投递前都会被调用一次；进程运行期间可执行文件不会变，缓存即可。
_ffmpeg_cache: str | None = None
_ffmpeg_checked = False


def probe_ffmpeg_path() -> str | None:
    """供启动预热调用（线程池内执行）：触发一次 ffmpeg 路径探测并缓存。"""
    return _ffmpeg_path()


def _ffmpeg_path() -> str | None:
    """返回 ffmpeg 可执行路径；未安装返回 None（结果进程内缓存）。"""
    global _ffmpeg_cache, _ffmpeg_checked
    if not _ffmpeg_checked:
        try:
            _ffmpeg_cache = shutil.which("ffmpeg")
        except Exception:  # noqa: BLE001
            _ffmpeg_cache = None
        _ffmpeg_checked = True
    return _ffmpeg_cache


async def _compress_to_mp3(local_path: str, bitrate_kbps: int = 128) -> str | None:
    """用 ffmpeg 把音频压成紧凑 mp3，返回新文件路径；ffmpeg 缺失或失败返回 None。

    输出放在源文件同目录，文件名 ``compact_<毫秒时间戳>.mp3``。
    任何失败路径（ffmpeg 缺失 / 非 0 退出 / 抛异常 / 未产出文件）都不留下半成品：
    ffmpeg 失败时可能已经写出部分字节，那是一个**未登记清理**的孤儿文件，必须删掉。
    """
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None
    out_path = os.path.join(
        os.path.dirname(local_path), f"compact_{int(time.time() * 1000)}.mp3"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", local_path, "-vn", "-b:a", f"{bitrate_kbps}k", "-ac", "2",
            out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return_code = await proc.wait()
    except Exception:
        _remove_file_quietly(out_path)
        return None
    if return_code != 0 or not os.path.exists(out_path):
        _remove_file_quietly(out_path)
        return None
    return out_path


async def _compact_for_aiocq(
    plugin,
    *,
    is_aiocq: bool,
    ffmpeg_compress: bool,
    ext: str,
    file_size: int,
    local_path: str,
    title: str,
    singer: str,
    compress_bitrate: int,
) -> str | None:
    """aiocqhttp(napcat)：无损/过大音频先压成紧凑 mp3，返回压缩产物路径。

    跨容器不共享文件系统，file:// 路径不可见(ENOENT)；Record 组件强制转 WAV+base64
    （载荷大→napcat 转码慢→WS 超时）。因此无损/过大音频先压成紧凑 mp3，语音走 silk、
    文件走 base64 内联直发（见 ``_aiocq_*``）。
    """
    if not (is_aiocq and ffmpeg_compress):
        return None
    lossless_or_big = ext.lower() in (".flac", ".wav", ".ogg", ".m4a") or file_size > AIOCQ_COMPRESS_THRESHOLD
    if not lossless_or_big:
        return None
    compact = await _compress_to_mp3(local_path, compress_bitrate)
    if compact:
        plugin._log_warn(
            f"aiocqhttp 无损/大文件已压成紧凑 mp3：{title} - {singer} {ext} 约 "
            f"{file_size / 1024 / 1024:.1f}MB"
        )
    return compact


async def _prepare_file_payload(
    plugin,
    *,
    want_file: bool,
    want_vocal: bool,
    is_aiocq: bool,
    file_size: int,
    ext: str,
    local_path: str,
    title: str,
    singer: str,
    aiocq_compact: str | None,
    file_blocked: bool,
    ffmpeg_compress: bool,
    compress_bitrate: int,
) -> tuple[str, str, bool] | None:
    """文件通道候选：(display, 路径, 已压缩标记)；None = 不发文件。"""
    if not want_file:
        return None
    if is_aiocq:
        # aiocqhttp：文件走 base64 直发，不依赖共享挂载；载荷用压缩 mp3 控制
        src = aiocq_compact or local_path
        display = build_music_filename(singer=singer, title=title, ext=".mp3") if aiocq_compact else build_music_filename(singer=singer, title=title, ext=ext)
        # 是否已压过如实上报，发送失败时才能按正确通道压一次再重试
        return (display, src, bool(aiocq_compact))
    if not file_blocked:
        return (build_music_filename(singer=singer, title=title, ext=ext), local_path, False)
    payload = None
    if ffmpeg_compress:
        compressed = await _compress_to_mp3(local_path, compress_bitrate)
        if compressed:
            payload = (build_music_filename(singer=singer, title=title, ext=".mp3"), compressed, True)
    if payload:
        plugin._log_warn(
            f"文件过大已 ffmpeg 压成紧凑 mp3 发送：{title} - {singer} "
            f"{ext} 约 {file_size / 1024 / 1024:.1f}MB"
        )
    else:
        plugin._log_warn(
            f"文件发送已跳过（QQ 官方）：{title} - {singer} {ext} 约 "
            f"{file_size / 1024 / 1024:.1f}MB，"
            f"{('改发语音(silk)转码版本' if want_vocal else '且语音发送未开启，音频文件未发送')}"
        )
    return payload


async def _send_media(plugin, event, pending_text: str, media_comp) -> None:
    """发送媒体组件；有文案时文案挂在媒体之前同一条消息里。"""
    comps = [plugin._plain(pending_text), media_comp] if pending_text else [media_comp]
    await plugin._send_chain(event, *comps)


async def _send_vocal(
    plugin, event, pending_text: str, voice_path: str, *, is_aiocq: bool, is_qqoff: bool
) -> tuple[str, bool]:
    """语音通道（Record）；返回 ``(更新后的 pending_text, 是否真的发出)``。

    ``pending_text`` 已随语音发出则为空串。第二项用于调用方如实汇总投递结果
    （双通道都没发出时不能谎报成功，见 ``_deliver_local_audio``）。
    """
    sent = False
    if is_aiocq:
        ok, reason = await _aiocq_send_record(event, pending_text, voice_path)
        if ok:
            sent = True
            pending_text = ""
        else:
            plugin._log_warn(f"aiocqhttp 语音直发失败（{reason}），退回 Record 组件")
    if not sent:
        try:
            await _send_media(plugin, event, pending_text, Record.fromFileSystem(voice_path))
            pending_text = ""
            sent = True
        except Exception as e:  # noqa: BLE001 语音失败不影响文件通道
            plugin._log_warn(f"语音发送失败{'（QQ 官方）' if is_qqoff else ''}: {e}")
    return pending_text, sent


async def _send_file_payload(
    plugin,
    event,
    *,
    file_payload: tuple[str, str, bool],
    pending_text: str,
    is_qqoff: bool,
    is_aiocq: bool,
    ffmpeg_compress: bool,
    compress_bitrate: int,
    keep_sec: int,
    title: str,
    singer: str,
) -> tuple[str, bool]:
    """发送文件；发送失败且未压缩过时 ffmpeg 压成紧凑 mp3 重试一次。

    aiocqhttp 的失败根因是「跨容器 file:// 路径不可见」，用普通 File 组件重试
    必然再次失败，因此重试同样走 aiocqhttp 专用发送（``_aiocq_send_file``）。
    返回 ``(更新后的 pending_text, 是否真的发出)``。
    """
    display, path, is_compressed = file_payload
    try:
        if is_aiocq:
            await _aiocq_send_file(event, pending_text, display, path)
            return "", True
        await _send_media(plugin, event, pending_text, File(display, file=path))
        return "", True
    except Exception as e:  # noqa: BLE001
        plugin._log_warn(f"文件发送失败{'（QQ 官方）' if is_qqoff else ''}: {e}")
    if is_compressed or not ffmpeg_compress:
        return pending_text, False
    compressed = await _compress_to_mp3(path, compress_bitrate)
    if not compressed:
        return pending_text, False
    plugin._log_warn("文件过大发送失败，已 ffmpeg 压成紧凑 mp3 重试")
    try:
        compact_display = build_music_filename(singer=singer, title=title, ext=".mp3")
        if is_aiocq:
            await _aiocq_send_file(event, pending_text, compact_display, compressed)
        else:
            await _send_media(plugin, event, pending_text, File(compact_display, file=compressed))
        return "", True
    except Exception as e2:  # noqa: BLE001
        plugin._log_warn(f"压缩版文件发送仍失败: {e2}")
        return pending_text, False
    finally:
        _schedule_cleanup(compressed, keep_sec)


def _schedule_delivery_cleanup(
    local_path: str, aiocq_compact: str | None, file_payload: tuple[str, str, bool] | None, keep_sec: int
) -> None:
    """投递结束统一调度临时文件清理（原文件 / 压缩产物 / 文件通道用的压缩件）。"""
    _schedule_cleanup(local_path, keep_sec)
    if aiocq_compact and aiocq_compact != local_path:
        _schedule_cleanup(aiocq_compact, keep_sec)
    if file_payload and file_payload[1] not in (local_path, aiocq_compact):
        _schedule_cleanup(file_payload[1], keep_sec)


async def _deliver_local_audio(
    plugin,
    event,
    *,
    cfg: dict,
    is_qqoff: bool,
    is_wxoc: bool,
    is_aiocq: bool = False,
    title: str,
    singer: str,
    local_path: str,
    file_size: int,
    pending_text: str = "",
) -> dict:
    """把已下载的本地音频按配置双通道投递（语音 silk + 文件），并调度清理临时文件。

    - 个人微信（weixin_oc）出站不支持 Record 语音 → 语音自动降级为文件发送
    - QQ 官方大文件（>10MB/FLAC）守卫：分片上传不可用时跳过文件仅发语音
    - 大文件/FLAC 无法作为文件发送时（守卫拦截、或发送失败）→ ffmpeg 压成紧凑 mp3 兜底
    - 文案（pending_text）挂在首个成功发送的媒体上；全部失败则单独补发文案兜底
    - 个人微信（weixin_oc）出站无媒体通道承载文案，改由调用方单独发一条文本消息
    - 语音/文件互不阻塞：任一失败不影响另一个；清理始终执行避免临时文件残留

    具体步骤按序拆到 ``_compact_for_aiocq`` / ``_prepare_file_payload`` /
    ``_send_vocal`` / ``_send_file_payload`` / ``_schedule_delivery_cleanup``。
    """
    # keep_sec 取值 + 后续准备逻辑全部纳入 try/finally：兜底调度必须无条件执行，否则
    # 已下载的临时音频永远不会进入清理调度（永久残留）。这里三处兜底：
    # ① 取值走 ``safe_int`` 回落 schema 默认值 60（WebUI 清空/手填非数字都不抛）；
    # ② 各变量先给默认值，保证 finally 里引用安全；③ 兜底调度在 finally 中无条件执行。
    # 注：用 ``safe_int`` 而不是 ``or 60``——``0`` 是合法取值（语义为尽快删除，
    # 实际延迟由 MIN_CLEANUP_DELAY_SEC 兜底），``or`` 会把显式配置的 0 改成 60 秒。
    # finally 要引用这些值：必须先给默认值——_prepare_delivery 抛异常或被取消
    # （插件重载时压缩协程被 cancel，CancelledError 不被其 except Exception 吞掉）时，
    # 已下载的 local_path 仍要登记清理，否则 UnboundLocalError 掩盖原异常且音频残留。
    keep_sec = 60
    aiocq_compact: str | None = None
    file_payload: tuple[str, str, bool] | None = None
    try:
        keep_sec, aiocq_compact, file_payload, want_vocal, ffmpeg_compress = await _prepare_delivery(
            plugin,
            cfg=cfg,
            is_qqoff=is_qqoff,
            is_wxoc=is_wxoc,
            is_aiocq=is_aiocq,
            title=title,
            singer=singer,
            local_path=local_path,
            file_size=file_size,
        )
        pending_text, vocal_sent, file_sent = await _send_channels(
            plugin,
            event,
            cfg=cfg,
            is_qqoff=is_qqoff,
            is_aiocq=is_aiocq,
            title=title,
            singer=singer,
            local_path=local_path,
            aiocq_compact=aiocq_compact,
            file_payload=file_payload,
            want_vocal=want_vocal,
            keep_sec=keep_sec,
            pending_text=pending_text,
            ffmpeg_compress=ffmpeg_compress,
        )
    finally:
        _schedule_delivery_cleanup(local_path, aiocq_compact, file_payload, keep_sec)

    if pending_text:
        await plugin._send_chain(event, plugin._plain(pending_text))

    # 双通道都没发出时不能谎报成功：``#kg听所有`` 的成功计数直接取 ``ok``。
    return {"ok": bool(vocal_sent or file_sent), "downloaded": True}


async def _prepare_delivery(
    plugin,
    *,
    cfg: dict,
    is_qqoff: bool,
    is_wxoc: bool,
    is_aiocq: bool,
    title: str,
    singer: str,
    local_path: str,
    file_size: int,
) -> tuple[int, str | None, tuple[str, str, bool] | None, bool]:
    """投递准备：通道判定 + QQ 官方大文件守卫 + aiocq 压缩 + 文件载荷。

    返回 ``(keep_sec, aiocq_compact, file_payload, want_vocal, ffmpeg_compress)``。
    keep_sec 走 ``safe_int``：WebUI 清空/填非数字回落 60（显式 ``0`` 保留「尽快删除」语义，
    实际延迟由 MIN_CLEANUP_DELAY_SEC 兜底；用 ``or 60`` 会把 0 错改成 60 秒）。
    """
    keep_sec = safe_int(cfg.get("keepFileSec"), 60)
    want_vocal = bool(cfg.get("sendVocal"))
    want_file = bool(cfg.get("uploadFile"))
    if is_wxoc:
        # 个人微信（weixin_oc）出站不支持 Record 语音 → 语音通道关闭、原语音需求并入文件
        want_vocal = False
        want_file = want_file or bool(cfg.get("sendVocal"))
    ext = os.path.splitext(local_path)[1] or ".mp3"

    # QQ 官方大文件（>10MB/FLAC）守卫：分片上传可用则放行；否则 ffmpeg 压成紧凑 mp3 兜底
    file_blocked = _should_block_qqofficial_file(
        is_qqoff=is_qqoff, want_file=want_file, file_size=file_size, ext=ext, cfg=cfg
    )
    ffmpeg_compress = cfg.get("ffmpegCompress", True) is not False and _ffmpeg_path() is not None
    compress_bitrate = max(32, cfg_int(cfg, "compressBitrate", 128))

    aiocq_compact = await _compact_for_aiocq(
        plugin,
        is_aiocq=is_aiocq,
        ffmpeg_compress=ffmpeg_compress,
        ext=ext,
        file_size=file_size,
        local_path=local_path,
        title=title,
        singer=singer,
        compress_bitrate=compress_bitrate,
    )
    file_payload = await _prepare_file_payload(
        plugin,
        want_file=want_file,
        want_vocal=want_vocal,
        is_aiocq=is_aiocq,
        file_size=file_size,
        ext=ext,
        local_path=local_path,
        title=title,
        singer=singer,
        aiocq_compact=aiocq_compact,
        file_blocked=file_blocked,
        ffmpeg_compress=ffmpeg_compress,
        compress_bitrate=compress_bitrate,
    )
    return keep_sec, aiocq_compact, file_payload, want_vocal, ffmpeg_compress


async def _send_channels(
    plugin,
    event,
    *,
    cfg: dict,
    is_qqoff: bool,
    is_aiocq: bool,
    title: str,
    singer: str,
    local_path: str,
    aiocq_compact: str | None,
    file_payload: tuple[str, str, bool] | None,
    want_vocal: bool,
    keep_sec: int,
    pending_text: str,
    ffmpeg_compress: bool,
) -> tuple[str, bool, bool]:
    """双通道发送（语音 silk + 文件），互不阻塞；返回 ``(pending_text, 语音发出, 文件发出)``。

    ``ffmpeg_compress`` 由 ``_prepare_delivery`` 统一推导（含 ffmpeg 存在性检查）后透传，
    避免两处各推一份将来漂移。
    """
    compress_bitrate = max(32, cfg_int(cfg, "compressBitrate", 128))
    vocal_sent = False
    file_sent = False
    if want_vocal:
        pending_text, vocal_sent = await _send_vocal(
            plugin, event, pending_text, aiocq_compact or local_path, is_aiocq=is_aiocq, is_qqoff=is_qqoff
        )
    if file_payload:
        pending_text, file_sent = await _send_file_payload(
            plugin,
            event,
            file_payload=file_payload,
            pending_text=pending_text,
            is_qqoff=is_qqoff,
            is_aiocq=is_aiocq,
            ffmpeg_compress=ffmpeg_compress,
            compress_bitrate=compress_bitrate,
            keep_sec=keep_sec,
            title=title,
            singer=singer,
        )
    return pending_text, vocal_sent, file_sent


async def deliver_song(
    plugin, event, song: dict, play: dict, *, cfg: dict, options: dict | None = None
) -> dict:
    options = options or {}
    title = song.get("name") or "未知歌曲"
    singer = song.get("artist") or "未知歌手"

    quality_label = trial_label(play)

    skip_text = options.get("skipTextInfo", False)
    skip_native = options.get("skipNativeCard", False)

    is_qqoff = _is_qqofficial(event) and cfg.get("qqofficialAdapt", True) is not False
    is_wxoc = _is_weixin_oc(event)
    is_aiocq = _is_aiocqhttp(event)

    # QQ 官方无 OneBot send_api，原生音乐卡本是 no-op，显式跳过避免误导
    allow_native = (not skip_native) and cfg.get("sendNativeCard") and not is_qqoff and not is_wxoc

    pending_text = ""
    if not skip_text and cfg.get("sendTextInfo", True):
        lines = [
            f"{cfg.get('identifyPrefix') or ''}酷狗音乐",
            f"♪ {title} - {singer}",
            f"专辑：{song['album']}" if song.get("album") else "",
            f"音质：{quality_label}" if quality_label else "",
            "" if play.get("url") else "⚠ 未获取到播放链，请 #kg登录",
        ]
        pending_text = "\n".join(x for x in lines if x)
        # 只有 QQ 官方（qq_official）需要把文案挂着一起发（合并消息，文案随媒体一起送达）；
        # 其余平台立即单独发一条文本消息。此前 wxoc 走的是「直接丢弃」分支，
        # 导致 sendTextInfo=true 在个人微信上完全无效（文案既没发也没挂到媒体上），
        # 与 README「weixin_oc 支持 Plain/Image/Video/File」矛盾 → 改为与其他平台同路径送达。
        if not is_qqoff:
            await plugin._send_chain(event, plugin._plain(pending_text))
            pending_text = ""

    if allow_native and song.get("hash"):
        await send_native_music_card(event, song["hash"])

    if not play.get("url"):
        return {"ok": False, "reason": "no_url"}

    need_download = cfg.get("sendVocal") or cfg.get("uploadFile")
    if not need_download:
        # QQ 官方把 pending_text 留着「挂媒体」一起发；但没有媒体要发时它会被直接丢弃，
        # 导致 sendTextInfo=true 在「只发文案」场景静默失效 → 此处补发。
        if pending_text:
            await plugin._send_chain(event, plugin._plain(pending_text))
        # 两个通道都关了：只可能发出文案，媒体确实没发 → 不能回 ok=True
        return {"ok": False, "reason": "no_channel", "downloaded": False}

    local_path = ""
    try:
        save_dir = await get_temp_dir()
        timeout = cfg_int(cfg, "downloadTimeout", 90000)
        dl = await download_audio(
            play["url"], save_dir, "kugou", timeout, play.get("quality") or cfg.get("quality") or ""
        )
        local_path = dl["filePath"]
    except Exception as err:
        # QQ 官方此前把歌曲信息文案挂着等合并发送，只发失败提示会把文案丢弃
        fail_text = f"下载音频失败：{err}\n可尝试 #kg登录 后重发，或换一首歌"
        fail_text = f"{pending_text}\n{fail_text}" if pending_text else fail_text
        await plugin._send_chain(event, plugin._plain(fail_text))
        return {"ok": False, "reason": "download_fail", "error": str(err)}

    return await _deliver_local_audio(
        plugin,
        event,
        cfg=cfg,
        is_qqoff=is_qqoff,
        is_wxoc=is_wxoc,
        is_aiocq=is_aiocq,
        title=title,
        singer=singer,
        local_path=local_path,
        file_size=int(dl.get("size", 0)),
        pending_text=pending_text,
    )
