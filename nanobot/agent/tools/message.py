"""
消息发送工具模块 (agent/tools/message.py)

模块职责：
    提供 MessageTool 工具，允许 Agent 通过 LLM function call 主动向用户发送消息。
    这是 Agent 与用户沟通的核心工具——当 LLM 决定需要回复用户时，就调用此工具。

在架构中的位置：
    MessageTool 通过一个异步回调函数（send_callback）将消息投递到消息总线，
    最终由具体的渠道实现（Telegram/Discord 等）将消息发送给用户。

    数据流：
    LLM 返回 tool_call("message", {content: "你好"})
      → MessageTool.execute()
        → 构建 OutboundMessage 对象
          → 调用 send_callback(msg)
            → 消息总线 → 渠道实现 → 用户收到消息

设计模式对比（Java 视角）：
    send_callback 是一个函数式回调（类似 Java 的 Consumer<OutboundMessage>）。
    这种设计将"消息发送"与"消息投递"解耦：
    MessageTool 只负责构建消息，不关心消息如何到达用户。

二开提示（语音多智能体）：
    如需支持语音回复，可在此工具的 execute() 中加入 TTS 转换逻辑，
    或新增 VoiceMessageTool 专门处理语音消息的发送。
"""

from typing import Any, Callable, Awaitable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class MessageTool(Tool):
    """
    消息发送工具，允许 Agent 主动向用户发送文本消息。

    工作原理：
    1. Agent 循环在启动时创建 MessageTool 并设置 send_callback
    2. LLM 决定回复用户时，生成 tool_call 调用此工具
    3. 工具构建 OutboundMessage 并通过回调发送

    类比 Java: 类似于一个消息发送服务（MessageService），
    通过依赖注入获取消息投递能力（send_callback）。
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = ""
    ):
        """
        初始化消息工具。

        参数:
            send_callback: 异步回调函数，负责将消息投递到消息总线
                          类型签名: async def callback(msg: OutboundMessage) -> None
            default_channel: 默认消息渠道（如 "telegram"）
            default_chat_id: 默认聊天/用户 ID
        """
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id

    def set_context(self, channel: str, chat_id: str) -> None:
        """
        设置当前消息上下文（当前正在对话的渠道和用户）。

        Agent 循环在处理每条入站消息时会调用此方法，
        确保回复消息发送到正确的渠道和用户。
        """
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """
        设置消息发送回调。

        在 Agent 初始化阶段调用，将消息总线的发送能力注入到工具中。
        """
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        **kwargs: Any
    ) -> str:
        """
        发送消息给用户。

        参数:
            content: 消息文本内容
            channel: 目标渠道（可选，默认使用当前上下文的渠道）
            chat_id: 目标聊天ID（可选，默认使用当前上下文的ID）

        返回:
            str: 发送结果描述

        执行流程:
            1. 确定目标渠道和聊天ID（优先使用显式参数，否则用默认值）
            2. 构建 OutboundMessage 数据对象
            3. 通过 send_callback 异步投递消息
        """
        # 优先使用显式指定的渠道/ID，否则回退到默认值
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        # 构建出站消息对象
        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content
        )

        try:
            # 通过回调函数将消息投递到消息总线
            await self._send_callback(msg)
            return f"Message sent to {channel}:{chat_id}"
        except Exception as e:
            return f"Error sending message: {str(e)}"