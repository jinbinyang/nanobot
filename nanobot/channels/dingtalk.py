"""
钉钉渠道实现 - 基于 Stream 模式的长连接通信。

本模块实现了钉钉机器人的消息收发功能：
- 入站：通过 dingtalk-stream SDK 的 WebSocket 长连接接收消息事件
- 出站：通过钉钉 HTTP API 发送消息（SDK 主要用于接收）

注意：当前仅支持单聊（1:1）对话，群聊消息会接收但回复以私信形式发送给发送者。

依赖：
- dingtalk-stream：钉钉 Stream 模式 SDK
- httpx：异步 HTTP 客户端，用于发送消息
"""

import asyncio
import json
import time
from typing import Any

from loguru import logger
import httpx

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import DingTalkConfig

# 尝试导入钉钉 Stream SDK，若未安装则提供占位符以避免模块级崩溃
try:
    from dingtalk_stream import (
        DingTalkStreamClient,
        Credential,
        CallbackHandler,
        CallbackMessage,
        AckMessage,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True  # 标记 SDK 是否可用
except ImportError:
    DINGTALK_AVAILABLE = False
    # 回退占位符，确保类定义不会在模块加载时崩溃
    CallbackHandler = object  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]


class NanobotDingTalkHandler(CallbackHandler):
    """
    钉钉 Stream SDK 标准回调处理器。

    职责：
    - 解析接收到的 Stream 消息
    - 提取文本内容和发送者信息
    - 将消息转发给 DingTalkChannel 进行后续处理

    继承自 SDK 的 CallbackHandler，通过 process() 方法处理每条消息。
    """

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel  # 关联的钉钉渠道实例

    async def process(self, message: CallbackMessage):
        """
        处理接收到的 Stream 消息。

        流程：
        1. 使用 SDK 的 ChatbotMessage 解析消息数据
        2. 提取文本内容（优先使用 SDK 解析，回退到原始字典）
        3. 获取发送者 ID 和昵称
        4. 创建异步任务转发消息到 Nanobot 消息总线

        参数:
            message: SDK 回调消息对象，包含原始消息数据

        返回:
            元组 (状态码, 描述) - 始终返回 OK 以避免钉钉服务器重试
        """
        try:
            # 使用 SDK 的 ChatbotMessage 进行健壮的消息解析
            chatbot_msg = ChatbotMessage.from_dict(message.data)

            # 提取文本内容；如果 SDK 对象为空，回退到原始字典
            content = ""
            if chatbot_msg.text:
                content = chatbot_msg.text.content.strip()
            if not content:
                content = message.data.get("text", {}).get("content", "").strip()

            # 空消息或不支持的消息类型，跳过处理
            if not content:
                logger.warning(
                    f"Received empty or unsupported message type: {chatbot_msg.message_type}"
                )
                return AckMessage.STATUS_OK, "OK"

            # 获取发送者信息（优先使用 staff_id，其次 sender_id）
            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"

            logger.info(f"Received DingTalk message from {sender_name} ({sender_id}): {content}")

            # 以非阻塞方式转发消息到 Nanobot
            # 保存任务引用以防止任务完成前被垃圾回收
            task = asyncio.create_task(
                self.channel._on_message(content, sender_id, sender_name)
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error(f"Error processing DingTalk message: {e}")
            # 返回 OK 以避免钉钉服务器进入重试循环
            return AckMessage.STATUS_OK, "Error"


class DingTalkChannel(BaseChannel):
    """
    钉钉渠道 - 基于 Stream 模式的长连接通信。

    架构设计：
    - 接收：通过 dingtalk-stream SDK 的 WebSocket 长连接接收事件
    - 发送：通过钉钉 HTTP API 直接调用（SDK 主要负责接收）

    Access Token 管理：
    - 自动获取和刷新 OAuth2 访问令牌
    - 提前 60 秒过期以确保安全

    注意：当前仅支持单聊（1:1），群聊消息会接收但回复以私信形式发送。
    """

    name = "dingtalk"  # 渠道标识名称

    def __init__(self, config: DingTalkConfig, bus: MessageBus):
        """
        初始化钉钉渠道。

        参数:
            config: 钉钉配置对象，包含 client_id 和 client_secret
            bus: 消息总线，用于发布和订阅消息事件
        """
        super().__init__(config, bus)
        self.config: DingTalkConfig = config
        self._client: Any = None  # dingtalk-stream SDK 客户端
        self._http: httpx.AsyncClient | None = None  # 异步 HTTP 客户端，用于发送消息

        # Access Token 管理
        self._access_token: str | None = None  # 当前有效的访问令牌
        self._token_expiry: float = 0  # 令牌过期时间戳

        # 后台任务引用集合，防止任务被垃圾回收
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """
        启动钉钉机器人（Stream 模式）。

        流程：
        1. 检查 SDK 是否可用
        2. 验证配置（client_id / client_secret）
        3. 创建 HTTP 客户端和 Stream 客户端
        4. 注册消息回调处理器
        5. 进入重连循环：Stream 断开后自动重连
        """
        try:
            if not DINGTALK_AVAILABLE:
                logger.error(
                    "DingTalk Stream SDK not installed. Run: pip install dingtalk-stream"
                )
                return

            if not self.config.client_id or not self.config.client_secret:
                logger.error("DingTalk client_id and client_secret not configured")
                return

            self._running = True
            self._http = httpx.AsyncClient()

            logger.info(
                f"Initializing DingTalk Stream Client with Client ID: {self.config.client_id}..."
            )
            # 创建认证凭据
            credential = Credential(self.config.client_id, self.config.client_secret)
            self._client = DingTalkStreamClient(credential)

            # 注册标准回调处理器
            handler = NanobotDingTalkHandler(self)
            self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

            logger.info("DingTalk bot started with Stream Mode")

            # 重连循环：SDK 退出或崩溃时自动重新启动 Stream
            while self._running:
                try:
                    await self._client.start()
                except Exception as e:
                    logger.warning(f"DingTalk stream error: {e}")
                if self._running:
                    logger.info("Reconnecting DingTalk stream in 5 seconds...")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.exception(f"Failed to start DingTalk channel: {e}")

    async def stop(self) -> None:
        """
        停止钉钉机器人。

        清理工作：
        1. 关闭共享 HTTP 客户端
        2. 取消所有未完成的后台任务
        """
        self._running = False
        # 关闭共享 HTTP 客户端
        if self._http:
            await self._http.aclose()
            self._http = None
        # 取消所有未完成的后台任务
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

    async def _get_access_token(self) -> str | None:
        """
        获取或刷新钉钉 Access Token。

        采用缓存策略：
        - 如果令牌未过期，直接返回缓存值
        - 否则调用钉钉 OAuth2 接口获取新令牌
        - 提前 60 秒设置过期时间，确保安全余量

        返回:
            有效的 Access Token 字符串，或获取失败时返回 None
        """
        # 缓存命中：令牌仍在有效期内
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        # 调用钉钉 OAuth2 接口获取新令牌
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot refresh token")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # 提前 60 秒过期，确保安全
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.error(f"Failed to get DingTalk access token: {e}")
            return None

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过钉钉 API 发送消息。

        使用 oToMessages/batchSend 接口发送单聊消息。
        消息格式为 Markdown，支持富文本展示。

        参数:
            msg: 出站消息对象，包含 chat_id（用户 staffId）和 content（消息内容）

        参考文档：
        https://open.dingtalk.com/document/orgapp/robot-batch-send-messages
        """
        token = await self._get_access_token()
        if not token:
            return

        # oToMessages/batchSend：向个人用户发送消息（单聊）
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"

        headers = {"x-acs-dingtalk-access-token": token}

        data = {
            "robotCode": self.config.client_id,
            "userIds": [msg.chat_id],  # chat_id 是用户的 staffId
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({
                "text": msg.content,
                "title": "Nanobot Reply",
            }),
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot send")
            return

        try:
            resp = await self._http.post(url, json=data, headers=headers)
            if resp.status_code != 200:
                logger.error(f"DingTalk send failed: {resp.text}")
            else:
                logger.debug(f"DingTalk message sent to {msg.chat_id}")
        except Exception as e:
            logger.error(f"Error sending DingTalk message: {e}")

    async def _on_message(self, content: str, sender_id: str, sender_name: str) -> None:
        """
        处理接收到的消息（由 NanobotDingTalkHandler 调用）。

        委托给 BaseChannel._handle_message()，该方法会执行 allow_from 权限检查
        后再将消息发布到消息总线。

        参数:
            content: 消息文本内容
            sender_id: 发送者 ID（staffId）
            sender_name: 发送者昵称
        """
        try:
            logger.info(f"DingTalk inbound: {content} from {sender_name}")
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender_id,  # 单聊场景下 chat_id == sender_id
                content=str(content),
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                },
            )
        except Exception as e:
            logger.error(f"Error publishing DingTalk message: {e}")