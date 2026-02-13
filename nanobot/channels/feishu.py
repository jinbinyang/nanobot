"""
飞书/Lark 渠道实现 - 基于 lark-oapi SDK 的 WebSocket 长连接通信。

本模块实现了飞书机器人的消息收发功能：
- 入站：通过 lark-oapi SDK 的 WebSocket 长连接接收消息事件（无需公网 IP 或 Webhook）
- 出站：通过飞书 Open API 发送交互式卡片消息（支持 Markdown + 表格）

架构特点：
- WebSocket 长连接运行在独立线程中（因为 lark SDK 使用同步 API）
- 通过 asyncio.run_coroutine_threadsafe 桥接同步线程与异步事件循环
- 内置消息去重机制（OrderedDict 缓存最近 1000 条消息 ID）
- 支持 Markdown 表格自动转换为飞书原生表格元素

依赖：
- lark-oapi：飞书官方 SDK（pip install lark-oapi）

二开提示：
- 如需添加语音消息支持，可在 _on_message 中处理 msg_type == "audio" 的情况
- 飞书支持富文本卡片，适合构建复杂的多 Agent 交互界面
"""

import asyncio
import json
import re
import threading
from collections import OrderedDict
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig

# 尝试导入飞书 SDK，若未安装则提供占位符以避免模块级崩溃
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True  # 标记 SDK 是否可用
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# 非文本消息类型的展示映射（收到这些类型时显示占位文本）
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class FeishuChannel(BaseChannel):
    """
    飞书/Lark 渠道 - 基于 WebSocket 长连接的消息通信。

    架构设计：
    - 接收：通过 lark-oapi SDK 的 WebSocket 长连接接收事件（无需公网 IP）
    - 发送：通过飞书 Open API 发送交互式卡片消息
    - 线程模型：WebSocket 在独立 daemon 线程中运行，通过 run_coroutine_threadsafe 桥接

    前置要求：
    - 在飞书开放平台创建应用，获取 App ID 和 App Secret
    - 启用机器人能力
    - 订阅 im.message.receive_v1 事件
    """
    
    name = "feishu"  # 渠道标识名称
    
    def __init__(self, config: FeishuConfig, bus: MessageBus):
        """
        初始化飞书渠道。

        参数:
            config: 飞书配置对象，包含 app_id、app_secret 等
            bus: 消息总线，用于发布和订阅消息事件
        """
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None              # lark SDK 客户端（用于发送消息）
        self._ws_client: Any = None           # WebSocket 客户端（用于接收消息）
        self._ws_thread: threading.Thread | None = None  # WebSocket 运行线程
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # 有序去重缓存
        self._loop: asyncio.AbstractEventLoop | None = None  # 主事件循环引用
    
    async def start(self) -> None:
        """
        启动飞书机器人（WebSocket 长连接模式）。

        流程：
        1. 检查 SDK 是否可用和配置是否完整
        2. 创建 Lark 客户端（用于发送消息 API 调用）
        3. 创建事件分发处理器（注册消息接收回调）
        4. 创建 WebSocket 客户端并在独立线程中启动
        5. 内置断线重连机制：连接断开后 5 秒自动重连
        """
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()  # 保存主事件循环引用，供同步线程使用
        
        # 创建 Lark 客户端（用于发送消息等 API 调用）
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        
        # 创建事件分发处理器（仅注册消息接收事件，忽略其他事件类型）
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",        # 加密密钥（可选）
            self.config.verification_token or "",  # 验证令牌（可选）
        ).register_p2_im_message_receive_v1(
            self._on_message_sync  # 注册消息接收的同步回调
        ).build()
        
        # 创建 WebSocket 客户端（长连接模式，无需公网 IP）
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # 在独立 daemon 线程中启动 WebSocket 客户端（带自动重连）
        def run_ws():
            """WebSocket 运行函数：断线后自动重连。"""
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning(f"Feishu WebSocket error: {e}")
                if self._running:
                    import time; time.sleep(5)  # 断线后等待 5 秒再重连
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")
        
        # 保持协程运行，直到 stop() 被调用
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """
        停止飞书机器人。

        清理 WebSocket 客户端连接。
        """
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")
    
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """
        同步添加消息表情回应（在线程池中运行）。

        通过飞书 API 为指定消息添加表情回应，常用于表示"已收到"。

        参数:
            message_id: 消息 ID
            emoji_type: 表情类型（如 THUMBSUP、OK、EYES 等）
        """
        try:
            # 构建添加表情回应的请求
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        异步添加消息表情回应（非阻塞）。

        将同步的 SDK 调用放入线程池执行，避免阻塞事件循环。

        常用表情类型: THUMBSUP（竖拇指）、OK、EYES（眼睛）、DONE（完成）、OnIt（在处理）、HEART（心）

        参数:
            message_id: 消息 ID
            emoji_type: 表情类型，默认为 THUMBSUP
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        # 在线程池中执行同步 SDK 调用
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    # 正则匹配 Markdown 表格（表头行 + 分隔行 + 数据行）
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """
        将 Markdown 表格解析为飞书原生表格元素。

        飞书卡片支持原生 table 标签，比纯文本表格展示效果更好。
        解析 Markdown 表格语法（| header | ... |）并转换为飞书 table JSON 结构。

        参数:
            table_text: Markdown 格式的表格文本

        返回:
            飞书 table 元素字典，解析失败返回 None
        """
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:  # 至少需要：表头、分隔符、一行数据
            return None
        split = lambda l: [c.strip() for c in l.strip("|").split("|")]
        headers = split(lines[0])      # 解析表头
        rows = [split(l) for l in lines[2:]]  # 解析数据行（跳过分隔行）
        # 构建飞书 table 列定义
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """
        将内容拆分为 Markdown + 表格元素列表（用于飞书卡片）。

        飞书卡片消息由多个元素组成，此方法将纯文本中的 Markdown 表格
        提取出来转换为原生表格元素，其余部分保持为 Markdown 元素。

        参数:
            content: 包含 Markdown 内容的原始文本

        返回:
            飞书卡片元素列表（每个元素是 markdown 或 table 类型的字典）
        """
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            # 表格之前的文本作为 markdown 元素
            before = content[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            # 将 Markdown 表格转换为飞书原生表格，转换失败则保留为 markdown
            elements.append(self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        # 处理最后一个表格之后的剩余文本
        remaining = content[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})
        return elements or [{"tag": "markdown", "content": content}]

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过飞书 API 发送消息。

        消息格式为交互式卡片（interactive），支持：
        - Markdown 富文本
        - 原生表格
        - 宽屏模式

        根据 chat_id 前缀自动判断接收者类型：
        - oc_ 开头：群聊 chat_id
        - ou_ 开头：用户 open_id

        参数:
            msg: 出站消息对象，包含 chat_id 和 content
        """
        if not self._client:
            logger.warning("Feishu client not initialized")
            return
        
        try:
            # 根据 chat_id 格式判断接收者类型
            # open_id 以 "ou_" 开头，chat_id 以 "oc_" 开头
            if msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"    # 群聊
            else:
                receive_id_type = "open_id"    # 个人
            
            # 构建交互式卡片内容（支持 Markdown + 表格混合）
            elements = self._build_card_elements(msg.content)
            card = {
                "config": {"wide_screen_mode": True},  # 宽屏模式
                "elements": elements,
            }
            content = json.dumps(card, ensure_ascii=False)
            
            # 构建发送消息的 API 请求
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(msg.chat_id)
                    .msg_type("interactive")  # 交互式卡片消息类型
                    .content(content)
                    .build()
                ).build()
            
            response = self._client.im.v1.message.create(request)
            
            if not response.success():
                logger.error(
                    f"Failed to send Feishu message: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
            else:
                logger.debug(f"Feishu message sent to {msg.chat_id}")
                
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        同步消息接收回调（从 WebSocket 线程调用）。

        由于 lark SDK 的 WebSocket 在独立线程中运行，此方法是同步的。
        通过 run_coroutine_threadsafe 将消息处理调度到主异步事件循环中。

        参数:
            data: 飞书消息接收事件对象
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)
    
    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """
        处理接收到的飞书消息（异步）。

        流程：
        1. 消息去重检查（基于 message_id）
        2. 过滤机器人自身消息
        3. 添加表情回应（表示"已收到"）
        4. 解析消息内容（文本 / 非文本类型）
        5. 转发到消息总线

        参数:
            data: 飞书消息接收事件对象
        """
        try:
            event = data.event
            message = event.message
            sender = event.sender
            
            # === 消息去重 ===
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return  # 重复消息，跳过
            self._processed_message_ids[message_id] = None
            
            # 缓存清理：超过 1000 条时保留最近 500 条
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)  # 移除最旧的
            
            # === 过滤机器人消息 ===
            sender_type = sender.sender_type
            if sender_type == "bot":
                return  # 忽略机器人自身发送的消息
            
            # === 提取发送者和聊天信息 ===
            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p"（单聊）或 "group"（群聊）
            msg_type = message.message_type
            
            # 添加表情回应表示"已收到"
            await self._add_reaction(message_id, "THUMBSUP")
            
            # === 解析消息内容 ===
            if msg_type == "text":
                try:
                    # 飞书文本消息的 content 是 JSON 格式：{"text": "实际内容"}
                    content = json.loads(message.content).get("text", "")
                except json.JSONDecodeError:
                    content = message.content or ""
            else:
                # 非文本消息类型，使用占位文本
                content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
            
            if not content:
                return
            
            # === 转发到消息总线 ===
            # 群聊回复到群，单聊回复到个人
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                }
            )
            
        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")