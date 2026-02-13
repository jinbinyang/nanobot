"""
消息事件类型定义模块 - 定义消息总线中传输的数据结构。

本模块定义了两个核心数据类：
- InboundMessage：入站消息（从渠道到 Agent）
- OutboundMessage：出站消息（从 Agent 到渠道）

这两个类是消息总线中所有数据流转的"货币"，所有渠道和 Agent
都通过这两个统一的数据结构进行通信，实现了渠道与 Agent 的解耦。

【Java 开发者类比】
- 使用 Python 的 @dataclass 装饰器，等价于 Java 的 record 类或 Lombok 的 @Data
- field(default_factory=...) 等价于 Java 中在构造器里 new ArrayList<>()
- @property 等价于 Java 的 getter 方法

【设计要点】
- session_key 属性将 channel 和 chat_id 组合为唯一会话标识，
  确保同一用户在同一渠道的消息被路由到同一个会话上下文中
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """
    入站消息 - 从聊天渠道接收到的用户消息。

    每当用户通过任何渠道（Telegram/Discord/Slack 等）发送消息时，
    对应的渠道适配器会将消息封装为 InboundMessage 并发布到消息总线。

    属性:
        channel: 消息来源渠道标识（如 'telegram', 'discord', 'slack', 'whatsapp'）
        sender_id: 发送者唯一标识（渠道内的用户 ID）
        chat_id: 聊天/频道唯一标识（区分不同的对话窗口）
        content: 消息文本内容
        timestamp: 消息时间戳，默认为当前时间
        media: 附带的媒体文件 URL 列表（图片、语音、文件等）
        metadata: 渠道特有的附加数据（如 Telegram 的 message_id、reply_to 等）
    """

    channel: str            # 来源渠道：telegram, discord, slack, whatsapp 等
    sender_id: str          # 发送者 ID：渠道内的用户唯一标识
    chat_id: str            # 聊天 ID：区分不同的对话窗口/群组
    content: str            # 消息正文：用户发送的文本内容
    timestamp: datetime = field(default_factory=datetime.now)  # 接收时间戳
    media: list[str] = field(default_factory=list)             # 媒体附件 URL 列表
    metadata: dict[str, Any] = field(default_factory=dict)     # 渠道特有的元数据

    @property
    def session_key(self) -> str:
        """
        生成唯一的会话标识键。

        格式为 "channel:chat_id"，例如 "telegram:123456"。
        用于在会话管理器中定位该消息所属的对话上下文，
        确保同一渠道同一聊天窗口的消息共享同一个会话。

        返回:
            格式化的会话标识字符串
        """
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """
    出站消息 - Agent 要发送到聊天渠道的回复消息。

    Agent 处理完用户请求后，将回复封装为 OutboundMessage 发布到消息总线，
    对应渠道的订阅者接收后将其转换为渠道原生格式发送给用户。

    属性:
        channel: 目标渠道标识（决定消息发往哪个渠道）
        chat_id: 目标聊天/频道标识（决定消息发给哪个对话窗口）
        content: 回复文本内容
        reply_to: 可选的引用消息 ID（用于实现"回复"效果）
        media: 附带的媒体文件 URL 列表
        metadata: 渠道特有的附加数据（如消息格式、按钮等）
    """

    channel: str                                                # 目标渠道标识
    chat_id: str                                                # 目标聊天 ID
    content: str                                                # 回复文本内容
    reply_to: str | None = None                                 # 引用的原始消息 ID（可选）
    media: list[str] = field(default_factory=list)              # 媒体附件 URL 列表
    metadata: dict[str, Any] = field(default_factory=dict)      # 渠道特有的元数据