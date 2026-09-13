"""酷狗音乐插件 —— AstrBot 音乐点歌/解析/探索/账号管理插件（模块化架构）。

架构：
    main.py            Star 插件主入口，生命周期管理与路由安装
    core/              业务服务层（MusicService、消息采集、状态管理）
    core/api/          酷狗音乐 API 客户端包（_core HTTP 会话 + 按域端点模块）
    core/cards.py      卡片数据构造与格式化
    core/delivery.py   音频下载、转码与分发交付
    core/render.py     Playwright HTML 渲染引擎
    core/messages.py   用户可见文案常量
    handlers/          声明式指令路由表（play/explore/detail/auth/system/share）
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

from .core.service import MusicService
from .handlers import ALL_ROUTES
from .handlers import install as install_routes

PLUGIN_NAME = "astrbot_plugin_kugoumusic"


class KugouMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = MusicService(self)

    # delivery.py 以 plugin._xxx 的形式调用投递辅助方法，实际实现挂在 service 上
    def _log_warn(self, msg: str):
        self.service.log_warn(msg)

    def _plain(self, text: str):
        return self.service.plain(text)

    # 仅供 core/delivery.py 使用的发送门面：**只在消息含媒体组件时**于失败处抛异常。
    # 投递层靠这个异常触发降级链（语音失败退回 Record 组件、文件失败用 ffmpeg 压成紧凑
    # mp3 重试）。纯文案（Plain）只是附带信息，失败不该中断投递——
    # 抛出去会让用户连音频带回复都拿不到。
    async def _send_chain(self, event, *components):
        has_media = any(c is not None and not isinstance(c, Plain) for c in components)
        return await self.service.send_chain(
            event, *components, raise_on_error=has_media
        )

    async def initialize(self):
        """启动时初始化服务层（例如设备注册等）。"""
        await self.service.initialize()

    async def terminate(self):
        """插件卸载/重载时清理轮询等任务。"""
        await self.service.terminate()


# 安装全部声明式路由（handlers/ 目录按业务域维护）
_installed = install_routes(KugouMusicPlugin, filter, __name__, ALL_ROUTES)
logger.info(f"[{PLUGIN_NAME}] 插件已加载，共注册 {_installed} 条指令路由")
