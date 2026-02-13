"""
QQ 渠道实现 - 基于 botpy SDK 的 WebSocket 连接。

本模块实现了 QQ 机器人的消息收发功能：
- 入站：通过 botpy SDK 的 WebSocket 连接接收 C2C（单聊）和私信消息
- 出站：通过 botpy SDK 的 API 发送 C2C 消息

架构特点：
- 使用 QQ 官方 botpy SDK，通过 WebSocket 实时接收事件
- 动态创建 botpy.Client 子类并绑定到渠道实例
- 内置消息去重（deque 最近 1000 条消息 ID）
- 内置断线重连机制（5 秒间隔）

依赖：
- qq-botpy：QQ 官方 Python SDK（pip install qq-botpy）

二开提示：
- 当前仅支持 C2C 单聊和私信，如需群聊可在 _Bot 中添加 on_group_at_message_create 回调
- QQ 机器人需在 QQ 开放平台注册并获取 app_id 和 secret
"""

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig

# 尝试导入 QQ botpy SDK，若未安装则提供占位符以避免模块级崩溃
try:
    import botpy
    from botpy.message import C2CMessage

    QQ_AVAILABLE = True  # 标记 SDK 是否可用
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None

# 类型检查时导入（仅用于 IDE 类型提示，运行时不执行）
if TYPE_CHECKING:
    from botpy.message import C2CMessage


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """
    动态创建绑定到指定渠道的 botpy Client 子类。

    这是一个工厂函数，通过闭包将 channel 实例绑定到动态创建的 Bot 类中。
    botpy SDK 要求用户继承 botpy.Client 并覆写事件回调方法。

    参数:
        channel: QQ 渠道实例，用于处理接收到的消息

    返回:
        动态创建的 botpy.Client 子类
    """
    # 声明机器人需要监听的事件意图
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        """动态创建的 QQ 机器人客户端，绑定到特定渠道实例。"""
        def __init__(self):
            super().__init__(intents=intents)

        async def on_ready(self):
            """机器人连接就绪回调。"""
            logger.info(f"QQ bot ready: {self.robot.name}")

        async def on_c2c_message_create(self, message: "C2CMessage"):
            """C2C 单聊消息接收回调。"""
            await channel._on_message(message)

        async def on_direct_message_create(self, message):
            """私信消息接收回调。"""
            await channel._on_message(message)

    return _Bot


class QQChannel(BaseChannel):
    """
    QQ 渠道 - 基于 botpy SDK 的 WebSocket 连接。

    架构设计：
    - 接收：通过 botpy SDK 的 WebSocket 连接接收事件
    - 发送：通过 botpy SDK 的 post_c2c_message API 发送消息
    - 去重：使用 deque 缓存最近 1000 条消息 ID

    支持的消息类型：
    - C2C 单聊消息（on_c2c_message_create）
    - 私信消息（on_direct_message_create）
    """

    name = "qq"  # 渠道标识名称

    def __init__(self, config: QQConfig, bus: MessageBus):
        """
        初始化 QQ 渠道。

        参数:
            config: QQ 配置对象，包含 app_id 和 secret
            bus: 消息总线，用于发布和订阅消息事件
        """
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._client: "botpy.Client | None" = None      # botpy 客户端实例
        self._processed_ids: deque = deque(maxlen=1000)  # 消息去重队列（最多保留 1000 条）
        self._bot_task: asyncio.Task | None = None       # 机器人运行任务

    async def start(self) -> None:
        """
        启动 QQ 机器人。

        流程：
        1. 检查 SDK 是否可用和配置是否完整
        2. 动态创建绑定到本渠道的 Bot 类
        3. 创建异步任务运行机器人（带自动重连）
        """
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        BotClass = _make_bot_class(self)  # 动态创建绑定到本渠道的 Bot 类
        self._client = BotClass()

        # 在独立的异步任务中运行机器人
        self._bot_task = asyncio.create_task(self._run_bot())
        logger.info("QQ bot started (C2C private message)")

    async def _run_bot(self) -> None:
        """
        运行机器人连接（带自动重连）。

        断线后每 5 秒自动尝试重新连接。
        """
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning(f"QQ bot error: {e}")
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """
        停止 QQ 机器人。

        取消运行中的机器人任务并等待其退出。
        """
        self._running = False
        if self._bot_task:
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
        logger.info("QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过 QQ API 发送 C2C 消息。

        使用 botpy SDK 的 post_c2c_message 接口发送纯文本消息。

        参数:
            msg: 出站消息对象，chat_id 为用户的 openid
        """
        if not self._client:
            logger.warning("QQ client not initialized")
            return
        try:
            await self._client.api.post_c2c_message(
                openid=msg.chat_id,   # 用户的 openid
                msg_type=0,           # 消息类型 0 = 纯文本
                content=msg.content,
            )
        except Exception as e:
            logger.error(f"Error sending QQ message: {e}")

    async def _on_message(self, data: "C2CMessage") -> None:
        """
        处理接收到的 QQ 消息。

        流程：
        1. 消息去重检查（基于消息 ID）
        2. 提取发送者 ID 和消息内容
        3. 转发到消息总线

        参数:
            data: botpy 的 C2CMessage 对象
        """
        try:
            # === 消息去重 ===
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            # === 提取发送者信息 ===
            author = data.author
            # 优先使用 id 属性，其次使用 user_openid
            user_id = str(getattr(author, 'id', None) or getattr(author, 'user_openid', 'unknown'))
            content = (data.content or "").strip()
            if not content:
                return

            # === 转发到消息总线 ===
            await self._handle_message(
                sender_id=user_id,
                chat_id=user_id,  # 单聊场景下 chat_id == sender_id
                content=content,
                metadata={"message_id": data.id},
            )
        except Exception as e:
            logger.error(f"Error handling QQ message: {e}")