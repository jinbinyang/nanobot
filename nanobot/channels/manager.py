"""
渠道管理器模块 - 统一管理所有消息渠道的生命周期和消息路由。

本模块是 nanobot 渠道层的"大管家"，负责：
1. 根据配置初始化所有已启用的渠道
2. 统一启动/停止所有渠道
3. 运行出站消息分发器，将 Agent 的回复路由到正确的渠道

【核心设计：出站消息分发】
ChannelManager 内部运行一个异步分发器任务（_dispatch_outbound），
不断从消息总线消费 OutboundMessage，根据 msg.channel 字段路由到
对应的渠道实例进行发送。

【Java 开发者类比】
- ChannelManager 相当于 Spring 的 ApplicationContext + MessageRouter
- _init_channels() 相当于 Spring 容器启动时的 Bean 初始化过程
- _dispatch_outbound() 相当于 JMS/Kafka 的 MessageListener 消费循环
- 延迟导入（lazy import）相当于 Spring 的懒加载（@Lazy）

【二开提示】
添加新渠道的步骤：
1. 在 config/schema.py 中添加新渠道的配置类
2. 创建 channels/your_channel.py 继承 BaseChannel
3. 在 _init_channels() 中添加初始化代码块
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Config


class ChannelManager:
    """
    渠道管理器 - 协调所有消息渠道的统一管理中心。

    职责：
    - 根据配置文件初始化所有已启用的渠道实例
    - 统一管理渠道的启动和停止
    - 运行出站消息分发器，将 Agent 回复路由到正确的渠道

    属性:
        config: 全局配置对象
        bus: 消息总线实例
        channels: 已初始化的渠道字典 {渠道名: 渠道实例}
        _dispatch_task: 出站消息分发器的异步任务句柄
    """

    def __init__(self, config: Config, bus: MessageBus):
        """
        初始化渠道管理器。

        在构造函数中立即调用 _init_channels() 初始化所有渠道，
        但不启动它们（启动在 start_all() 中进行）。

        参数:
            config: 全局配置对象，包含所有渠道的配置
            bus: 消息总线实例，所有渠道共享
        """
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None

        # 在构造时立即初始化渠道（但不启动）
        self._init_channels()

    def _init_channels(self) -> None:
        """
        根据配置初始化所有已启用的渠道。

        采用"延迟导入"模式：只有当某个渠道在配置中启用时，
        才导入对应的渠道模块。这样做的好处是：
        - 未启用的渠道不需要安装其依赖包
        - 启动速度更快（不加载无用模块）
        - ImportError 不会影响其他渠道的正常使用

        每个渠道的初始化块结构一致：
        1. 检查配置是否启用（config.channels.xxx.enabled）
        2. 尝试导入渠道类
        3. 创建实例并注册到 self.channels 字典
        4. 导入失败时仅打印警告，不中断其他渠道
        """

        # ===== Telegram 渠道 =====
        if self.config.channels.telegram.enabled:
            try:
                from nanobot.channels.telegram import TelegramChannel
                self.channels["telegram"] = TelegramChannel(
                    self.config.channels.telegram,
                    self.bus,
                    # Telegram 渠道需要 Groq API 密钥用于语音转文字
                    groq_api_key=self.config.providers.groq.api_key,
                )
                logger.info("Telegram channel enabled")
            except ImportError as e:
                logger.warning(f"Telegram channel not available: {e}")

        # ===== WhatsApp 渠道 =====
        if self.config.channels.whatsapp.enabled:
            try:
                from nanobot.channels.whatsapp import WhatsAppChannel
                self.channels["whatsapp"] = WhatsAppChannel(
                    self.config.channels.whatsapp, self.bus
                )
                logger.info("WhatsApp channel enabled")
            except ImportError as e:
                logger.warning(f"WhatsApp channel not available: {e}")

        # ===== Discord 渠道 =====
        if self.config.channels.discord.enabled:
            try:
                from nanobot.channels.discord import DiscordChannel
                self.channels["discord"] = DiscordChannel(
                    self.config.channels.discord, self.bus
                )
                logger.info("Discord channel enabled")
            except ImportError as e:
                logger.warning(f"Discord channel not available: {e}")

        # ===== 飞书（Feishu）渠道 =====
        if self.config.channels.feishu.enabled:
            try:
                from nanobot.channels.feishu import FeishuChannel
                self.channels["feishu"] = FeishuChannel(
                    self.config.channels.feishu, self.bus
                )
                logger.info("Feishu channel enabled")
            except ImportError as e:
                logger.warning(f"Feishu channel not available: {e}")

        # ===== Mochat 渠道（轻量聊天服务）=====
        if self.config.channels.mochat.enabled:
            try:
                from nanobot.channels.mochat import MochatChannel

                self.channels["mochat"] = MochatChannel(
                    self.config.channels.mochat, self.bus
                )
                logger.info("Mochat channel enabled")
            except ImportError as e:
                logger.warning(f"Mochat channel not available: {e}")

        # ===== 钉钉（DingTalk）渠道 =====
        if self.config.channels.dingtalk.enabled:
            try:
                from nanobot.channels.dingtalk import DingTalkChannel
                self.channels["dingtalk"] = DingTalkChannel(
                    self.config.channels.dingtalk, self.bus
                )
                logger.info("DingTalk channel enabled")
            except ImportError as e:
                logger.warning(f"DingTalk channel not available: {e}")

        # ===== Email 渠道 =====
        if self.config.channels.email.enabled:
            try:
                from nanobot.channels.email import EmailChannel
                self.channels["email"] = EmailChannel(
                    self.config.channels.email, self.bus
                )
                logger.info("Email channel enabled")
            except ImportError as e:
                logger.warning(f"Email channel not available: {e}")

        # ===== Slack 渠道 =====
        if self.config.channels.slack.enabled:
            try:
                from nanobot.channels.slack import SlackChannel
                self.channels["slack"] = SlackChannel(
                    self.config.channels.slack, self.bus
                )
                logger.info("Slack channel enabled")
            except ImportError as e:
                logger.warning(f"Slack channel not available: {e}")

        # ===== QQ 渠道 =====
        if self.config.channels.qq.enabled:
            try:
                from nanobot.channels.qq import QQChannel
                self.channels["qq"] = QQChannel(
                    self.config.channels.qq,
                    self.bus,
                )
                logger.info("QQ channel enabled")
            except ImportError as e:
                logger.warning(f"QQ channel not available: {e}")

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """
        启动单个渠道并捕获异常。

        将每个渠道的启动包装在 try-except 中，确保一个渠道的启动失败
        不会影响其他渠道。

        参数:
            name: 渠道名称
            channel: 渠道实例
        """
        try:
            await channel.start()
        except Exception as e:
            logger.error(f"Failed to start channel {name}: {e}")

    async def start_all(self) -> None:
        """
        启动所有渠道和出站消息分发器。

        启动顺序：
        1. 先启动出站消息分发器（确保 Agent 的回复能被路由）
        2. 并行启动所有渠道（使用 asyncio.gather 并发执行）

        注意：渠道的 start() 方法通常是阻塞式的长期运行任务，
        所以用 create_task 将每个渠道放入独立的异步任务中。
        """
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # 先启动出站消息分发器，确保回复通道就绪
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # 并行启动所有渠道
        tasks = []
        for name, channel in self.channels.items():
            logger.info(f"Starting {name} channel...")
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        # 等待所有渠道任务完成（正常情况下它们会一直运行直到被取消）
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """
        停止所有渠道和分发器。

        停止顺序：
        1. 先停止分发器（停止消费出站消息）
        2. 逐个停止所有渠道
        """
        logger.info("Stopping all channels...")

        # 取消出站消息分发器
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass  # 取消是预期行为，忽略异常

        # 逐个停止渠道，确保每个都尝试清理
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info(f"Stopped {name} channel")
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")

    async def _dispatch_outbound(self) -> None:
        """
        出站消息分发器 - 持续运行的消息路由循环。

        工作流程：
        1. 从消息总线消费 OutboundMessage（带1秒超时，避免阻塞）
        2. 根据 msg.channel 字段找到对应的渠道实例
        3. 调用渠道的 send() 方法发送消息
        4. 处理各种异常情况（未知渠道、发送失败等）

        该方法类似 Java 中的 MessageListener.onMessage() 消费循环，
        或 Kafka Consumer 的 poll() 循环。
        """
        logger.info("Outbound dispatcher started")

        while True:
            try:
                # 从总线消费出站消息，1秒超时避免永久阻塞
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )

                # 根据消息的 channel 字段路由到对应渠道
                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error(f"Error sending to {msg.channel}: {e}")
                else:
                    # 目标渠道不存在（可能配置错误或渠道未启用）
                    logger.warning(f"Unknown channel: {msg.channel}")

            except asyncio.TimeoutError:
                continue  # 超时无消息，继续下一轮轮询
            except asyncio.CancelledError:
                break  # 收到取消信号，退出分发循环

    def get_channel(self, name: str) -> BaseChannel | None:
        """
        根据名称获取渠道实例。

        参数:
            name: 渠道名称（如 "telegram"、"discord"）

        返回:
            渠道实例，不存在时返回 None
        """
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """
        获取所有渠道的运行状态。

        返回:
            渠道状态字典，格式为 {渠道名: {"enabled": bool, "running": bool}}
        """
        return {
            name: {
                "enabled": True,       # 在 channels 字典中的都是已启用的
                "running": channel.is_running  # 查询实际运行状态
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """
        获取所有已启用的渠道名称列表。

        返回:
            渠道名称列表，如 ["telegram", "discord", "slack"]
        """
        return list(self.channels.keys())