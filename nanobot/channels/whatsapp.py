"""
WhatsApp 渠道实现 - 基于 Node.js 桥接服务的消息通信。

本模块实现了 WhatsApp 消息机器人的收发功能：
- 入站：通过 WebSocket 连接到 Node.js 桥接服务接收消息
- 出站：通过 WebSocket 发送消息指令给桥接服务

架构特点：
- 桥接模式：Python <-> WebSocket <-> Node.js Bridge <-> WhatsApp Web
- Node.js 桥接使用 @whiskeysockets/baileys 库处理 WhatsApp Web 协议
- 内置断线重连机制（5 秒间隔）
- 支持认证令牌（bridge_token）进行桥接服务鉴权

依赖：
- websockets：Python WebSocket 客户端库
- 外部 Node.js 桥接服务（需独立部署运行）

二开提示：
- 语音消息目前仅能接收但不支持转写，可集成 transcription 模块实现
- 桥接模式适合做中间层扩展，如添加消息队列缓冲、多实例负载均衡等
"""

import asyncio
import json
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WhatsAppConfig


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp 渠道 - 通过 Node.js 桥接服务通信。

    架构设计：
    - 不直接与 WhatsApp 服务器通信，而是通过中间桥接层
    - 桥接层使用 @whiskeysockets/baileys 处理 WhatsApp Web 协议
    - Python 端通过 WebSocket 与桥接层进行 JSON 消息交换

    消息协议（Python <-> Bridge）：
    - auth：发送认证令牌
    - message：接收到的用户消息
    - send：发送消息给用户
    - status：连接状态更新
    - qr：首次登录的二维码（需在桥接终端扫码）
    - error：错误信息
    """
    
    name = "whatsapp"  # 渠道标识名称
    
    def __init__(self, config: WhatsAppConfig, bus: MessageBus):
        """
        初始化 WhatsApp 渠道。

        参数:
            config: WhatsApp 配置对象，包含 bridge_url 和 bridge_token
            bus: 消息总线，用于发布和订阅消息事件
        """
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self._ws = None           # WebSocket 连接对象
        self._connected = False   # 桥接服务连接状态
    
    async def start(self) -> None:
        """
        启动 WhatsApp 渠道（连接到 Node.js 桥接服务）。

        流程：
        1. 建立 WebSocket 连接到桥接服务
        2. 发送认证令牌（如果配置了）
        3. 进入消息监听循环
        4. 连接断开时自动重连（5 秒间隔）
        """
        import websockets
        
        bridge_url = self.config.bridge_url
        
        logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")
        
        self._running = True
        
        # 断线重连循环
        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # 如果配置了认证令牌，先发送认证
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")
                    
                    # 持续监听桥接服务发来的消息
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")
                    
            except asyncio.CancelledError:
                break  # 任务被取消，退出循环
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning(f"WhatsApp bridge connection error: {e}")
                
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)  # 断线后等待 5 秒再重连
    
    async def stop(self) -> None:
        """
        停止 WhatsApp 渠道。

        关闭 WebSocket 连接并清理状态。
        """
        self._running = False
        self._connected = False
        
        if self._ws:
            await self._ws.close()
            self._ws = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """
        通过 WhatsApp 桥接服务发送消息。

        消息格式为 JSON：{"type": "send", "to": "<chat_id>", "text": "<内容>"}

        参数:
            msg: 出站消息对象，包含 chat_id（用户 LID）和 content（消息内容）
        """
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return
        
        try:
            payload = {
                "type": "send",          # 消息类型：发送
                "to": msg.chat_id,       # 接收者 ID
                "text": msg.content      # 消息文本
            }
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")
    
    async def _handle_bridge_message(self, raw: str) -> None:
        """
        处理从桥接服务收到的消息。

        根据消息类型（type 字段）分发处理：
        - message：用户发来的消息，转发到消息总线
        - status：连接状态更新（connected/disconnected）
        - qr：首次登录二维码（需在桥接终端扫码）
        - error：桥接服务报告的错误

        参数:
            raw: 原始 JSON 字符串
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from bridge: {raw[:100]}")
            return
        
        msg_type = data.get("type")
        
        if msg_type == "message":
            # === 处理用户发来的消息 ===
            # 旧版格式：手机号样式，如 <phone>@s.whatsapp.net（已过时）
            pn = data.get("pn", "")
            # 新版 LID 格式
            sender = data.get("sender", "")
            content = data.get("content", "")
            
            # 提取用户 ID（优先使用手机号，其次用 LID）
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info(f"Sender {sender}")
            
            # 处理语音消息（目前仅记录，暂不支持转写）
            if content == "[Voice Message]":
                logger.info(f"Voice message received from {sender_id}, but direct download from bridge is not yet supported.")
                content = "[Voice Message: Transcription not available for WhatsApp yet]"
            
            # 转发到消息总线
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # 使用完整 LID 作为回复地址
                content=content,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False)  # 是否为群聊消息
                }
            )
        
        elif msg_type == "status":
            # === 连接状态更新 ===
            status = data.get("status")
            logger.info(f"WhatsApp status: {status}")
            
            if status == "connected":
                self._connected = True    # 桥接已连接
            elif status == "disconnected":
                self._connected = False   # 桥接已断开
        
        elif msg_type == "qr":
            # === 首次登录二维码 ===
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")
        
        elif msg_type == "error":
            # === 错误信息 ===
            logger.error(f"WhatsApp bridge error: {data.get('error')}")
