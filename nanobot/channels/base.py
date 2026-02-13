"""
渠道基类模块 - 定义所有消息渠道的统一接口。

本模块提供了 BaseChannel 抽象基类，所有具体渠道（Telegram、Discord 等）
都必须继承此基类并实现其抽象方法。这是"策略模式"（Strategy Pattern）
在 nanobot 中的典型应用。

【核心抽象方法】
- start(): 启动渠道，开始监听消息（长期运行的异步任务）
- stop(): 停止渠道，释放资源
- send(): 向渠道发送出站消息

【公共能力】
- is_allowed(): 基于白名单的权限控制
- _handle_message(): 消息预处理与转发（权限检查 → 构造 InboundMessage → 发布到总线）

【Java 开发者类比】
- BaseChannel 相当于 Java 的 abstract class + interface
- 抽象方法（@abstractmethod）相当于 Java 接口中的未实现方法
- _handle_message() 相当于 Template Method 模式中的模板方法
- is_allowed() 相当于 Spring Security 的 AccessDecisionVoter
"""

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


class BaseChannel(ABC):
    """
    消息渠道抽象基类 - 所有渠道实现的统一契约。

    每个渠道代表一个外部即时通讯平台的接入点，负责：
    1. 接收来自平台的用户消息
    2. 进行权限校验
    3. 将消息标准化为 InboundMessage 发布到消息总线
    4. 从消息总线接收 OutboundMessage 并发送到平台

    属性:
        name: 渠道标识名（如 "telegram"、"discord"），用于消息路由
        config: 渠道特定的配置对象（每个渠道的配置结构不同）
        bus: 消息总线实例，用于发布/订阅消息
        _running: 渠道运行状态标志
    """

    name: str = "base"  # 子类必须覆盖此属性为具体渠道名

    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化渠道。

        参数:
            config: 渠道特定的配置对象（如 TelegramConfig、DiscordConfig 等）
            bus: 消息总线实例，所有渠道共享同一个总线
        """
        self.config = config
        self.bus = bus
        self._running = False  # 初始状态为未运行

    @abstractmethod
    async def start(self) -> None:
        """
        启动渠道并开始监听消息。

        这应该是一个长期运行的异步任务（类似 Java 的 daemon 线程），
        负责：
        1. 连接到聊天平台（如建立 WebSocket 连接、启动 HTTP 轮询等）
        2. 持续监听用户发来的消息
        3. 收到消息后调用 _handle_message() 进行预处理和转发

        子类必须实现此方法。
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        停止渠道并清理资源。

        子类应在此方法中：
        1. 断开与平台的连接
        2. 取消所有后台任务
        3. 释放占用的资源（文件句柄、网络连接等）
        """
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        通过该渠道发送出站消息。

        由 ChannelManager 的出站消息分发器调用，
        将 Agent 的回复发送到对应的聊天平台。

        参数:
            msg: 标准化的出站消息对象，包含目标聊天 ID 和消息内容
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """
        检查发送者是否有权限使用该机器人。

        基于配置中的 allow_from 白名单进行校验：
        - 白名单为空 → 允许所有人（开放模式）
        - 白名单非空 → 只允许名单中的用户

        特殊处理：支持 "|" 分隔的复合 sender_id（某些平台的群聊+用户组合格式），
        会逐段检查是否在白名单中。

        参数:
            sender_id: 发送者标识符

        返回:
            True 表示允许访问，False 表示拒绝
        """
        allow_list = getattr(self.config, "allow_from", [])

        # 白名单为空时采用开放模式，允许所有人
        if not allow_list:
            return True

        sender_str = str(sender_id)
        # 直接匹配
        if sender_str in allow_list:
            return True
        # 复合 ID 格式（如 "group_id|user_id"）的逐段匹配
        if "|" in sender_str:
            for part in sender_str.split("|"):
                if part and part in allow_list:
                    return True
        return False

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None
    ) -> None:
        """
        处理来自聊天平台的入站消息（模板方法）。

        这是所有渠道共用的消息预处理流程：
        1. 权限检查：验证发送者是否在白名单中
        2. 消息标准化：将平台特定的消息格式转换为统一的 InboundMessage
        3. 发布到总线：通过 MessageBus 将消息传递给 Agent 处理

        各渠道子类在 start() 中收到平台消息后，调用此方法即可。

        参数:
            sender_id: 发送者标识符（平台用户 ID）
            chat_id: 聊天/频道标识符（平台聊天 ID）
            content: 消息文本内容
            media: 可选的媒体文件 URL 列表（图片、语音等）
            metadata: 可选的渠道特定元数据（如消息类型、回复引用等）
        """
        # 权限校验：不在白名单中的发送者直接拒绝
        if not self.is_allowed(sender_id):
            logger.warning(
                f"Access denied for sender {sender_id} on channel {self.name}. "
                f"Add them to allowFrom list in config to grant access."
            )
            return

        # 构造标准化的入站消息对象
        msg = InboundMessage(
            channel=self.name,           # 来源渠道标识
            sender_id=str(sender_id),    # 统一转为字符串
            chat_id=str(chat_id),        # 统一转为字符串
            content=content,
            media=media or [],           # 无媒体时使用空列表
            metadata=metadata or {}      # 无元数据时使用空字典
        )

        # 发布到消息总线，触发 Agent 处理
        await self.bus.publish_inbound(msg)

    @property
    def is_running(self) -> bool:
        """
        检查渠道是否正在运行。

        返回:
            True 表示渠道已启动且正在监听消息
        """
        return self._running