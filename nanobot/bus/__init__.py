"""
消息总线模块 - 实现渠道与 Agent 核心之间的解耦通信。

本模块是 nanobot 的"中枢神经系统"，采用异步消息队列模式（类似 Java 中的
BlockingQueue + 观察者模式），将消息的"接收"和"处理"完全解耦：

消息流向：
  用户消息 → 渠道(Channel) → InboundMessage → 消息总线 → Agent 处理
  Agent 回复 → OutboundMessage → 消息总线 → 渠道(Channel) → 用户

【Java 开发者类比】
- MessageBus 类似于 Spring 的 ApplicationEventPublisher + @EventListener
- InboundMessage 类似于一个入站 DTO（Data Transfer Object）
- OutboundMessage 类似于一个出站 DTO
- 整体模式类似于 JMS（Java Message Service）的简化版

【二开提示】
在多 Agent 系统中，消息总线可以扩展为 Agent 间通信的中枢：
管家 Agent 接收用户消息后，通过总线将任务分发给领域 Agent，
领域 Agent 处理完后通过总线将结果返回给管家 Agent。
"""

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
