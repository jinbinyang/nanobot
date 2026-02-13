"""
Slack 渠道实现模块 - 基于 Socket Mode 的实时通信。

本模块实现了 nanobot 与 Slack 平台的对接，采用 Socket Mode（WebSocket）模式，
无需公网 IP 和 HTTP 端点，通过 Slack 的 WebSocket 连接接收事件。

【Socket Mode 简述】
Slack 提供两种接收事件的方式：
1. HTTP Events API：需要公网可访问的 HTTP 端点（类似 Webhook）
2. Socket Mode：通过 WebSocket 接收事件，无需公网 IP
nanobot 选择 Socket Mode，部署更简单，适合本地开发和内网环境。

【核心功能】
1. 接收私信（DM）和频道中的 @提及 消息
2. 智能消息过滤：支持 open/mention/allowlist 三种群组响应策略
3. 消息线程（Thread）支持：频道消息在线程中回复，私信不使用线程
4. 收到消息时自动添加 :eyes: 表情反应作为已读确认
5. 防重复处理：@提及 同时触发 message 和 app_mention 事件时只处理一次

【Java 开发者类比】
Socket Mode 类似于 Spring WebSocket 的 STOMP 协议客户端：
- SocketModeClient 类似于 WebSocketStompClient
- 事件监听器类似于 @MessageMapping 注解方法
- AsyncWebClient 类似于 RestTemplate / WebClient（用于发送消息）
- 权限策略类似于 Spring Security 的访问控制

【Slack 的两个 Token】
- Bot Token (xoxb-...)：用于发送消息、调用 Web API
- App Token (xapp-...)：用于建立 Socket Mode 连接
两者缺一不可，需要在 Slack App 管理后台分别生成。

【二开提示】
Slack 的 Block Kit 提供了丰富的 UI 组件（按钮、下拉菜单、模态框等），
可以为多 Agent 系统设计交互式 UI，让用户通过按钮选择不同领域的 Agent。
"""

import asyncio
import re
from typing import Any

from loguru import logger
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import SlackConfig


class SlackChannel(BaseChannel):
    """
    Slack 渠道实现 - 基于 Socket Mode（WebSocket）。

    通过 Slack SDK 的 SocketModeClient 建立 WebSocket 连接接收事件，
    通过 AsyncWebClient 调用 Slack Web API 发送消息。

    支持三种群组响应策略：
    - open：响应所有消息
    - mention：仅响应 @提及 机器人的消息
    - allowlist：仅响应白名单频道中的消息

    属性:
        config: Slack 渠道配置（token、模式、策略等）
        _web_client: Slack Web API 异步客户端（用于发送消息）
        _socket_client: Socket Mode 客户端（用于接收事件）
        _bot_user_id: 机器人自身的用户 ID（用于识别 @提及 和过滤自身消息）
    """

    name = "slack"

    def __init__(self, config: SlackConfig, bus: MessageBus):
        """
        初始化 Slack 渠道。

        参数:
            config: Slack 配置（包含 bot_token、app_token、响应策略等）
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None      # Web API 客户端
        self._socket_client: SocketModeClient | None = None  # Socket Mode 客户端
        self._bot_user_id: str | None = None                 # 机器人自身的用户 ID

    async def start(self) -> None:
        """
        启动 Slack Socket Mode 客户端。

        启动流程：
        1. 验证 bot_token 和 app_token 配置
        2. 创建 Web API 客户端和 Socket Mode 客户端
        3. 注册事件监听器
        4. 通过 auth_test 获取机器人自身 ID
        5. 建立 WebSocket 连接并进入主循环
        """
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot/app token not configured")
            return
        if self.config.mode != "socket":
            logger.error(f"Unsupported Slack mode: {self.config.mode}")
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        # 注册事件监听器（类似 Spring 的 @EventListener）
        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # 通过 auth_test 接口获取机器人自身的用户 ID（用于后续识别 @提及）
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info(f"Slack bot connected as {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"Slack auth_test failed: {e}")

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止 Slack 客户端，关闭 WebSocket 连接。"""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                logger.warning(f"Slack socket close failed: {e}")
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过 Slack Web API 发送消息。

        智能线程回复策略：
        - 频道/群组消息：在线程（Thread）中回复，避免刷屏
        - 私信（DM）：直接回复，不使用线程

        参数:
            msg: 出站消息对象，chat_id 为 Slack 频道/DM 的 ID
        """
        if not self._web_client:
            logger.warning("Slack client not running")
            return
        try:
            # 从消息元数据中提取 Slack 特有的线程和频道类型信息
            slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
            thread_ts = slack_meta.get("thread_ts")      # 线程时间戳（Slack 用时间戳标识线程）
            channel_type = slack_meta.get("channel_type")  # 频道类型：im(私信) / channel / group
            # 仅在频道/群组消息中使用线程回复，私信直接回复
            use_thread = thread_ts and channel_type != "im"
            await self._web_client.chat_postMessage(
                channel=msg.chat_id,
                text=msg.content or "",
                thread_ts=thread_ts if use_thread else None,
            )
        except Exception as e:
            logger.error(f"Error sending Slack message: {e}")

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """
        处理 Socket Mode 收到的事件请求。

        这是所有 Slack 事件的入口点，处理流程：
        1. 过滤非 events_api 类型的请求
        2. 立即发送 ACK 确认（Slack 要求3秒内确认，否则会重发）
        3. 提取事件数据，过滤无关事件
        4. 权限检查和去重处理
        5. 添加 :eyes: 表情反应作为已读确认
        6. 转发到消息总线

        参数:
            client: Socket Mode 客户端实例
            req: Socket Mode 请求对象
        """
        if req.type != "events_api":
            return

        # 立即发送 ACK 确认（Slack 要求3秒内响应，否则会重发事件）
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        # 只处理普通消息和 @提及 事件，忽略其他事件类型
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        # 过滤机器人和系统消息（有 subtype 的都不是普通用户消息）
        if event.get("subtype"):
            return
        # 过滤自身消息，防止自我响应循环
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        # 防重复处理：Slack 在频道中 @提及 机器人时会同时触发 message 和 app_mention
        # 两个事件。这里优先处理 app_mention，跳过包含 @提及 的普通 message。
        text = event.get("text") or ""
        if event_type == "message" and self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            return

        # 调试日志：记录事件基本信息
        logger.debug(
            "Slack event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            event.get("subtype"),
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""

        if not self._is_allowed(sender_id, chat_id, channel_type):
            return

        if channel_type != "im" and not self._should_respond_in_channel(event_type, text, chat_id):
            return

        text = self._strip_bot_mention(text)

        # 获取线程时间戳（优先使用 thread_ts，回退到消息自身的 ts）
        thread_ts = event.get("thread_ts") or event.get("ts")
        # 给触发消息添加 :eyes: 表情反应，作为已读确认（尽力而为，失败不影响主流程）
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name="eyes",
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug(f"Slack reactions_add failed: {e}")

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=text,
            metadata={
                "slack": {
                    "event": event,
                    "thread_ts": thread_ts,
                    "channel_type": channel_type,
                }
            },
        )

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        """
        检查消息发送者是否有权与机器人交互。

        根据消息来源（私信/群组）和配置策略进行权限判断：
        - 私信：检查 DM 是否启用 + allowlist 策略
        - 群组：检查 group_policy 的 allowlist 策略

        参数:
            sender_id: 发送者用户 ID
            chat_id: 频道/DM 的 ID
            channel_type: 频道类型（im=私信，channel=频道，group=群组）

        返回:
            True 表示允许交互
        """
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from
            return True

        # 群组/频道消息的权限检查
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _should_respond_in_channel(self, event_type: str, text: str, chat_id: str) -> bool:
        """
        判断是否应该在频道/群组中回复消息。

        根据 group_policy 配置决定：
        - open：响应所有消息
        - mention：仅响应 @提及 机器人的消息
        - allowlist：仅响应白名单频道中的消息

        参数:
            event_type: 事件类型（message/app_mention）
            text: 消息文本
            chat_id: 频道 ID

        返回:
            True 表示应该回复
        """
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            if event_type == "app_mention":
                return True
            return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def _strip_bot_mention(self, text: str) -> str:
        """
        去除消息文本中的 @机器人 提及标记。

        Slack 中 @提及 的格式为 <@USER_ID>，需要从文本中移除
        以获得干净的用户意图文本。

        参数:
            text: 原始消息文本

        返回:
            去除 @提及 标记后的文本
        """
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()
