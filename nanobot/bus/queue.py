"""
异步消息队列模块 - 消息总线的核心实现。

本模块实现了 MessageBus 类，它是 nanobot 中所有消息流转的中枢。
采用生产者-消费者模式，基于 Python asyncio.Queue 实现异步消息传递：

入站流程（用户 → Agent）：
  渠道适配器 → publish_inbound() → inbound 队列 → consume_inbound() → Agent 循环

出站流程（Agent → 用户）：
  Agent 循环 → publish_outbound() → outbound 队列 → dispatch_outbound() → 渠道回调

【Java 开发者类比】
- asyncio.Queue 类似于 Java 的 LinkedBlockingQueue
- publish/consume 模式类似于 Java 的 BlockingQueue.put()/take()
- subscribe_outbound + dispatch_outbound 模式类似于 Spring 的 @EventListener 机制
- dispatch_outbound 后台任务类似于 Java 的 ExecutorService 中的消费者线程

【核心设计】
出站消息采用"发布-订阅"模式：每个渠道通过 subscribe_outbound() 注册回调，
dispatch_outbound() 后台任务持续消费出站队列并按渠道名称路由到对应的回调函数。
这意味着新增渠道只需注册一个订阅回调，无需修改总线代码（开闭原则）。
"""

import asyncio
from typing import Callable, Awaitable

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    异步消息总线 - 解耦聊天渠道与 Agent 核心的通信中枢。

    消息总线维护两个异步队列：
    - inbound 队列：存放从各渠道收到的用户消息，等待 Agent 消费
    - outbound 队列：存放 Agent 的回复消息，等待分发到对应渠道

    出站消息通过"订阅者模式"分发：各渠道预先注册回调函数，
    后台 dispatch 任务持续消费 outbound 队列并调用对应渠道的回调。

    属性:
        inbound: 入站消息异步队列（渠道 → Agent）
        outbound: 出站消息异步队列（Agent → 渠道）
        _outbound_subscribers: 出站消息订阅者字典 {渠道名: [回调函数列表]}
        _running: 分发器运行状态标志
    """

    def __init__(self):
        """初始化消息总线，创建入站和出站两个异步队列。"""
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()    # 入站队列
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()  # 出站队列
        # 出站订阅者字典：key 为渠道名，value 为异步回调函数列表
        self._outbound_subscribers: dict[str, list[Callable[[OutboundMessage], Awaitable[None]]]] = {}
        self._running = False  # 分发器运行标志

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """
        发布入站消息（渠道 → Agent）。

        渠道适配器收到用户消息后调用此方法将消息放入入站队列，
        Agent 的主循环会从队列中取出并处理。

        参数:
            msg: 入站消息对象
        """
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """
        消费下一条入站消息（阻塞等待）。

        Agent 主循环调用此方法获取待处理的用户消息。
        如果队列为空，会异步阻塞直到有新消息到达。

        返回:
            下一条入站消息
        """
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """
        发布出站消息（Agent → 渠道）。

        Agent 处理完用户请求后调用此方法将回复放入出站队列，
        dispatch_outbound 后台任务会取出并分发到对应渠道。

        参数:
            msg: 出站消息对象
        """
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """
        消费下一条出站消息（阻塞等待）。

        一般不直接调用此方法，而是通过 dispatch_outbound() 自动消费。
        保留此方法用于需要手动控制出站消息处理的场景。

        返回:
            下一条出站消息
        """
        return await self.outbound.get()

    def subscribe_outbound(
        self,
        channel: str,
        callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        """
        订阅指定渠道的出站消息。

        各渠道在初始化时调用此方法注册自己的消息发送回调。
        当有该渠道的出站消息时，回调函数会被自动调用。
        同一渠道可以注册多个回调（如同时记录日志和发送消息）。

        参数:
            channel: 渠道名称（如 'telegram', 'discord'）
            callback: 异步回调函数，接收 OutboundMessage 参数
        """
        if channel not in self._outbound_subscribers:
            self._outbound_subscribers[channel] = []
        self._outbound_subscribers[channel].append(callback)

    async def dispatch_outbound(self) -> None:
        """
        出站消息分发器（后台常驻任务）。

        持续从 outbound 队列中取出消息，根据消息的 channel 字段
        找到对应的订阅者回调函数并调用。这是一个无限循环任务，
        通常通过 asyncio.create_task() 在后台运行。

        使用 wait_for 超时机制（1秒）避免在 stop() 时长时间阻塞，
        确保能及时响应停止信号。

        错误处理：单个回调的异常不会中断整个分发循环，
        仅记录错误日志后继续处理下一条消息。
        """
        self._running = True
        while self._running:
            try:
                # 等待最多1秒获取下一条出站消息，超时则重新检查 _running 标志
                msg = await asyncio.wait_for(self.outbound.get(), timeout=1.0)
                # 查找该渠道的所有订阅者
                subscribers = self._outbound_subscribers.get(msg.channel, [])
                for callback in subscribers:
                    try:
                        await callback(msg)  # 调用渠道的发送回调
                    except Exception as e:
                        # 单个回调异常不影响其他订阅者和后续消息
                        logger.error(f"Error dispatching to {msg.channel}: {e}")
            except asyncio.TimeoutError:
                continue  # 超时无消息，继续循环检查 _running 状态

    def stop(self) -> None:
        """
        停止出站消息分发器。

        设置 _running 为 False，dispatch_outbound 循环会在
        下次超时检查时退出。
        """
        self._running = False

    @property
    def inbound_size(self) -> int:
        """
        获取待处理的入站消息数量。

        返回:
            入站队列中的消息数量
        """
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """
        获取待分发的出站消息数量。

        返回:
            出站队列中的消息数量
        """
        return self.outbound.qsize()