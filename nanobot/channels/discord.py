"""
Discord 渠道实现模块 - 基于 Discord Gateway WebSocket 协议。

本模块实现了 nanobot 与 Discord 平台的对接，直接使用 Discord Gateway
WebSocket API 而非高级 SDK（如 discord.py），保持了极简的依赖。

【核心功能】
1. 通过 WebSocket 连接 Discord Gateway 接收实时消息
2. 自动心跳保活（HEARTBEAT）
3. 断线自动重连（带5秒延迟）
4. 通过 REST API 发送消息（支持速率限制重试）
5. 下载消息中的附件文件
6. "正在输入..."状态指示器

【Discord Gateway 协议简述】
Discord 使用基于 WebSocket 的 Gateway 协议进行实时通信：
- op=10 (HELLO): 服务器下发心跳间隔，客户端开始心跳 + 身份验证
- op=2 (IDENTIFY): 客户端发送 token 进行身份验证
- op=0 (DISPATCH): 服务器推送事件（如 MESSAGE_CREATE）
- op=1 (HEARTBEAT): 心跳包
- op=7 (RECONNECT): 服务器要求重连
- op=9 (INVALID SESSION): 会话无效，需要重新连接

【Java 开发者类比】
类似于直接使用 Java-WebSocket 库连接 Discord，而非使用 JDA 等高级框架：
- Gateway 连接类似于 WebSocketClient
- 心跳机制类似于 ScheduledExecutorService 定时任务
- REST API 调用类似于 HttpClient 发送 POST 请求
- 速率限制重试类似于带退避的重试策略（Resilience4j）

【二开提示】
Discord 支持丰富的交互形式（Slash Commands、Buttons、Embeds 等），
如果需要为多 Agent 系统设计更好的 UI，可以利用 Discord 的 Interaction 机制
让用户选择不同的领域 Agent 进行对话。
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import websockets
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import DiscordConfig


# Discord REST API 基础 URL（v10 版本）
DISCORD_API_BASE = "https://discord.com/api/v10"
# 附件下载大小限制：20MB
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class DiscordChannel(BaseChannel):
    """
    Discord 渠道实现 - 基于 Gateway WebSocket 协议。

    使用原生 WebSocket 连接 Discord Gateway，配合 REST API 发送消息。
    支持自动心跳、断线重连、附件下载和速率限制处理。

    属性:
        config: Discord 渠道配置（token、gateway URL、intents 等）
        _ws: WebSocket 连接实例
        _seq: 最新的事件序列号（用于心跳和断线恢复）
        _heartbeat_task: 心跳定时任务
        _typing_tasks: 频道ID → "正在输入"指示器任务映射
        _http: HTTP 异步客户端（用于 REST API 调用和文件下载）
    """

    name = "discord"

    def __init__(self, config: DiscordConfig, bus: MessageBus):
        """
        初始化 Discord 渠道。

        参数:
            config: Discord 配置（包含 bot token、gateway URL 等）
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: DiscordConfig = config
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._seq: int | None = None  # Gateway 事件序列号，心跳时需要回传
        self._heartbeat_task: asyncio.Task | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """
        启动 Discord Gateway 连接。

        采用外层无限循环实现断线自动重连：
        连接断开后等待5秒重新连接，直到 _running 被设为 False。
        """
        if not self.config.token:
            logger.error("Discord bot token not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)

        # 外层重连循环
        while self._running:
            try:
                logger.info("Connecting to Discord gateway...")
                async with websockets.connect(self.config.gateway_url) as ws:
                    self._ws = ws
                    await self._gateway_loop()  # 内层消息处理循环
            except asyncio.CancelledError:
                break  # 被取消时直接退出
            except Exception as e:
                logger.warning(f"Discord gateway error: {e}")
                if self._running:
                    logger.info("Reconnecting to Discord gateway in 5 seconds...")
                    await asyncio.sleep(5)  # 断线重连延迟

    async def stop(self) -> None:
        """
        停止 Discord 渠道。

        按顺序清理：心跳任务 → 输入指示器 → WebSocket → HTTP 客户端。
        """
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过 Discord REST API 发送消息。

        支持消息引用（回复）和速率限制自动重试（最多3次）。

        参数:
            msg: 出站消息对象，chat_id 为 Discord 频道 ID
        """
        if not self._http:
            logger.warning("Discord HTTP client not initialized")
            return

        url = f"{DISCORD_API_BASE}/channels/{msg.chat_id}/messages"
        payload: dict[str, Any] = {"content": msg.content}

        # 如果是回复消息，添加消息引用
        if msg.reply_to:
            payload["message_reference"] = {"message_id": msg.reply_to}
            payload["allowed_mentions"] = {"replied_user": False}  # 不 @ 被回复的用户

        headers = {"Authorization": f"Bot {self.config.token}"}

        try:
            # 最多重试3次（处理速率限制）
            for attempt in range(3):
                try:
                    response = await self._http.post(url, headers=headers, json=payload)
                    if response.status_code == 429:
                        # 429 = 速率限制，按服务器指示的时间等待后重试
                        data = response.json()
                        retry_after = float(data.get("retry_after", 1.0))
                        logger.warning(f"Discord rate limited, retrying in {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    response.raise_for_status()
                    return  # 发送成功
                except Exception as e:
                    if attempt == 2:
                        logger.error(f"Error sending Discord message: {e}")
                    else:
                        await asyncio.sleep(1)  # 非速率限制错误也稍等重试
        finally:
            # 无论成功失败，都停止输入指示器
            await self._stop_typing(msg.chat_id)

    async def _gateway_loop(self) -> None:
        """
        Gateway 主消息循环 - 处理来自 Discord 的 WebSocket 消息。

        根据操作码（opcode）分发处理：
        - op=10 (HELLO): 启动心跳 + 发送身份验证
        - op=0 (DISPATCH): 分发事件（READY、MESSAGE_CREATE 等）
        - op=7 (RECONNECT): 退出循环触发重连
        - op=9 (INVALID SESSION): 退出循环触发重连
        """
        if not self._ws:
            return

        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from Discord gateway: {raw[:100]}")
                continue

            op = data.get("op")        # 操作码
            event_type = data.get("t") # 事件类型（仅 op=0 时有值）
            seq = data.get("s")        # 事件序列号
            payload = data.get("d")    # 事件数据

            # 更新序列号（心跳时需要回传最新序列号）
            if seq is not None:
                self._seq = seq

            if op == 10:
                # HELLO: 服务器下发心跳间隔，启动心跳并发送身份验证
                interval_ms = payload.get("heartbeat_interval", 45000)
                await self._start_heartbeat(interval_ms / 1000)  # 毫秒转秒
                await self._identify()
            elif op == 0 and event_type == "READY":
                # 身份验证成功，Gateway 就绪
                logger.info("Discord gateway READY")
            elif op == 0 and event_type == "MESSAGE_CREATE":
                # 收到新消息
                await self._handle_message_create(payload)
            elif op == 7:
                # 服务器要求重连
                logger.info("Discord gateway requested reconnect")
                break
            elif op == 9:
                # 会话无效，需要重新连接
                logger.warning("Discord gateway invalid session")
                break

    async def _identify(self) -> None:
        """
        发送 IDENTIFY 消息进行身份验证。

        包含 bot token、权限意图（intents）和客户端标识信息。
        """
        if not self._ws:
            return

        identify = {
            "op": 2,  # IDENTIFY 操作码
            "d": {
                "token": self.config.token,
                "intents": self.config.intents,  # 订阅的事件类型位掩码
                "properties": {
                    "os": "nanobot",
                    "browser": "nanobot",
                    "device": "nanobot",
                },
            },
        }
        await self._ws.send(json.dumps(identify))

    async def _start_heartbeat(self, interval_s: float) -> None:
        """
        启动或重启心跳循环。

        心跳是维持 Gateway 连接的关键机制，
        客户端必须按服务器指定的间隔发送心跳包，否则会被断开。

        参数:
            interval_s: 心跳间隔（秒）
        """
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        async def heartbeat_loop() -> None:
            """心跳循环：定期发送 op=1 心跳包"""
            while self._running and self._ws:
                payload = {"op": 1, "d": self._seq}  # 心跳需要携带最新序列号
                try:
                    await self._ws.send(json.dumps(payload))
                except Exception as e:
                    logger.warning(f"Discord heartbeat failed: {e}")
                    break
                await asyncio.sleep(interval_s)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def _handle_message_create(self, payload: dict[str, Any]) -> None:
        """
        处理 MESSAGE_CREATE 事件（收到新消息）。

        处理流程：
        1. 过滤机器人消息（防止自我响应）
        2. 检查白名单权限
        3. 提取文本内容
        4. 下载附件文件（大小限制 20MB）
        5. 启动输入指示器
        6. 转发到消息总线

        参数:
            payload: Discord 消息事件的完整数据
        """
        author = payload.get("author") or {}
        if author.get("bot"):
            return  # 忽略机器人消息，防止自我响应循环

        sender_id = str(author.get("id", ""))
        channel_id = str(payload.get("channel_id", ""))
        content = payload.get("content") or ""

        if not sender_id or not channel_id:
            return

        # 检查发送者是否在白名单中
        if not self.is_allowed(sender_id):
            return

        content_parts = [content] if content else []
        media_paths: list[str] = []
        media_dir = Path.home() / ".nanobot" / "media"

        # 处理消息附件（图片、文件等）
        for attachment in payload.get("attachments") or []:
            url = attachment.get("url")
            filename = attachment.get("filename") or "attachment"
            size = attachment.get("size") or 0
            if not url or not self._http:
                continue
            # 检查文件大小限制
            if size and size > MAX_ATTACHMENT_BYTES:
                content_parts.append(f"[attachment: {filename} - too large]")
                continue
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                # 用附件 ID + 文件名命名，避免文件名冲突
                file_path = media_dir / f"{attachment.get('id', 'file')}_{filename.replace('/', '_')}"
                resp = await self._http.get(url)
                resp.raise_for_status()
                file_path.write_bytes(resp.content)
                media_paths.append(str(file_path))
                content_parts.append(f"[attachment: {file_path}]")
            except Exception as e:
                logger.warning(f"Failed to download Discord attachment: {e}")
                content_parts.append(f"[attachment: {filename} - download failed]")

        # 检查是否为回复消息（获取被回复消息的 ID）
        reply_to = (payload.get("referenced_message") or {}).get("id")

        # 启动输入指示器
        await self._start_typing(channel_id)

        # 转发到消息总线
        await self._handle_message(
            sender_id=sender_id,
            chat_id=channel_id,
            content="\n".join(p for p in content_parts if p) or "[empty message]",
            media=media_paths,
            metadata={
                "message_id": str(payload.get("id", "")),
                "guild_id": payload.get("guild_id"),  # Discord 服务器 ID
                "reply_to": reply_to,
            },
        )

    async def _start_typing(self, channel_id: str) -> None:
        """
        启动频道的"正在输入"指示器（每8秒发送一次 REST 请求）。

        参数:
            channel_id: Discord 频道 ID
        """
        await self._stop_typing(channel_id)

        async def typing_loop() -> None:
            """循环发送 typing 状态"""
            url = f"{DISCORD_API_BASE}/channels/{channel_id}/typing"
            headers = {"Authorization": f"Bot {self.config.token}"}
            while self._running:
                try:
                    await self._http.post(url, headers=headers)
                except Exception:
                    pass  # typing 失败不影响核心功能
                await asyncio.sleep(8)  # Discord typing 状态持续约10秒

        self._typing_tasks[channel_id] = asyncio.create_task(typing_loop())

    async def _stop_typing(self, channel_id: str) -> None:
        """
        停止频道的"正在输入"指示器。

        参数:
            channel_id: Discord 频道 ID
        """
        task = self._typing_tasks.pop(channel_id, None)
        if task:
            task.cancel()