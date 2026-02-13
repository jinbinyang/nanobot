"""
Agent 主循环模块 —— nanobot 的核心处理引擎。

本模块是整个 nanobot 系统的"心脏"，实现了完整的 Agent 处理流水线：
  用户消息 → 上下文构建 → LLM 推理 → 工具调用 → 响应返回

核心类 AgentLoop 采用经典的 ReAct（Reasoning + Acting）范式：
1. 从消息总线（MessageBus）接收用户消息
2. 通过 ContextBuilder 构建包含历史、记忆、技能的上下文
3. 调用 LLM 获取推理结果
4. 如果 LLM 请求调用工具，执行工具并将结果反馈给 LLM
5. 重复步骤3-4直到 LLM 给出最终回复（或达到最大迭代次数）

【Java 开发者类比】
- AgentLoop 类似于 Spring 中的核心 Service，持有所有依赖并协调它们
- run() 方法类似于消息监听器（@KafkaListener），持续消费消息
- _process_message() 类似于业务处理方法，包含完整的请求处理逻辑
- 工具注册表（ToolRegistry）类似于 Spring 的 Bean 容器，按名称查找执行

【二开提示】
要实现"核心对话管家 + 多领域Agent"架构，关键改造点：
1. 在 _process_message() 中加入意图识别/路由逻辑
2. 利用已有的 SubagentManager 和 SpawnTool 分派任务给子Agent
3. 在上下文中注入"管家"角色的系统提示词
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger  # loguru: Python 高性能日志库，比 Java 的 SLF4J 更简洁

from nanobot.bus.events import InboundMessage, OutboundMessage  # 消息总线的入站/出站消息类型
from nanobot.bus.queue import MessageBus  # 消息总线，类似 Java 的消息队列
from nanobot.providers.base import LLMProvider  # LLM 提供者抽象基类
from nanobot.agent.context import ContextBuilder  # 上下文构建器
from nanobot.agent.tools.registry import ToolRegistry  # 工具注册表
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool  # 文件系统工具
from nanobot.agent.tools.shell import ExecTool  # Shell 命令执行工具
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool  # Web 搜索和抓取工具
from nanobot.agent.tools.message import MessageTool  # 消息发送工具
from nanobot.agent.tools.spawn import SpawnTool  # 子Agent 生成工具
from nanobot.agent.tools.cron import CronTool  # 定时任务工具
from nanobot.agent.memory import MemoryStore  # 记忆存储
from nanobot.agent.subagent import SubagentManager  # 子Agent 管理器
from nanobot.session.manager import SessionManager  # 会话管理器


class AgentLoop:
    """
    Agent 主循环 —— nanobot 的核心处理引擎。

    职责：
    1. 持续监听消息总线（MessageBus）上的入站消息
    2. 为每条消息构建完整的 LLM 上下文（系统提示词 + 历史对话 + 当前消息）
    3. 调用 LLM 进行推理，处理工具调用（ReAct 循环）
    4. 将最终响应发送回消息总线

    核心属性：
    - bus: 消息总线，负责消息的收发
    - provider: LLM 提供者（通过 LiteLLM 支持多种模型）
    - workspace: 工作区路径，存放配置、记忆、技能等
    - tools: 工具注册表，管理所有可用工具
    - sessions: 会话管理器，维护多轮对话状态
    - subagents: 子Agent管理器，处理异步后台任务

    【Java 类比】可以理解为一个带有消息监听器的 Service Bean，
    持有 ToolRegistry（类似 ApplicationContext）和 SessionManager（类似 HttpSession）。
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
    ):
        """
        初始化 Agent 主循环。

        参数：
            bus: 消息总线实例，用于接收入站消息和发送出站消息
            provider: LLM 提供者实例，封装了对 AI 模型的调用
            workspace: 工作区目录路径，所有文件操作的根目录
            model: 指定使用的模型名称（如 "gpt-4o"），为 None 时使用 provider 的默认模型
            max_iterations: Agent 循环最大迭代次数（防止无限循环），默认20次
            memory_window: 记忆窗口大小，超过此数量的消息会被合并归档，默认50条
            brave_api_key: Brave Search API 密钥，用于 Web 搜索工具
            exec_config: Shell 命令执行配置（超时时间等）
            cron_service: 定时任务服务实例（可选）
            restrict_to_workspace: 是否限制文件操作只能在工作区内（安全沙箱模式）
            session_manager: 会话管理器实例（可选，为 None 时自动创建）
        """
        # 延迟导入，避免循环依赖（Python 常用技巧，类似 Java 的 @Lazy 注解）
        from nanobot.config.schema import ExecToolConfig
        from nanobot.cron.service import CronService
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()  # 未指定模型时使用默认模型
        self.max_iterations = max_iterations
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()  # 使用默认配置
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        
        # 初始化核心组件
        self.context = ContextBuilder(workspace)  # 上下文构建器
        self.sessions = session_manager or SessionManager(workspace)  # 会话管理器
        self.tools = ToolRegistry()  # 工具注册表（类似 Spring 的 Bean 容器）
        self.subagents = SubagentManager(  # 子Agent管理器
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        
        self._running = False  # 运行状态标志
        self._register_default_tools()  # 注册默认工具集
    
    def _register_default_tools(self) -> None:
        """
        注册默认工具集。

        将所有内置工具注册到工具注册表中，使 LLM 可以调用它们。
        类似于 Java Spring 中在 @Configuration 类里注册 Bean。

        工具分类：
        - 文件工具：读/写/编辑/列目录
        - Shell工具：执行系统命令
        - Web工具：搜索和抓取网页
        - 消息工具：向聊天渠道发送消息
        - 生成工具：创建子Agent执行后台任务
        - 定时工具：创建定时任务
        """
        # --- 文件工具 ---
        # 如果启用了工作区限制，则文件操作只能在 workspace 目录内进行
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))
        
        # --- Shell 工具 ---
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
        ))
        
        # --- Web 工具 ---
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # --- 消息工具 ---
        # 通过回调函数将消息发送到消息总线（依赖注入模式）
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # --- 子Agent 生成工具 ---
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)
        
        # --- 定时任务工具（仅在配置了 cron_service 时注册） ---
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
    
    async def run(self) -> None:
        """
        启动 Agent 主循环，持续从消息总线消费并处理消息。

        这是一个无限循环（类似 Java 中的 while(true) 消息监听器），
        通过 asyncio.wait_for 设置1秒超时来实现非阻塞轮询。

        处理流程：
        1. 从消息总线获取下一条入站消息（1秒超时）
        2. 调用 _process_message() 处理消息
        3. 将响应发布到消息总线
        4. 如果出错，发送错误提示给用户
        """
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # 等待下一条消息，1秒超时（超时后继续循环检查 _running 状态）
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # 处理消息
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # 发送错误响应给用户（优雅降级）
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue  # 超时正常，继续轮询
    
    def stop(self) -> None:
        """
        停止 Agent 主循环。

        设置 _running 标志为 False，主循环将在下次迭代时退出。
        """
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage, session_key: str | None = None) -> OutboundMessage | None:
        """
        处理单条入站消息 —— Agent 的核心业务逻辑。

        这是整个系统最重要的方法，实现了完整的 ReAct 循环：
        1. 处理系统消息（子Agent回报）
        2. 获取/创建会话
        3. 处理斜杠命令（/new, /help）
        4. 必要时合并归档历史消息（记忆管理）
        5. 构建 LLM 上下文
        6. 执行 Agent 循环（LLM推理 → 工具调用 → 反思 → 重复）
        7. 保存会话并返回响应

        参数：
            msg: 入站消息对象（包含渠道、发送者、内容等信息）
            session_key: 会话键覆盖（用于 process_direct 直接调用场景）

        返回：
            OutboundMessage 响应消息，或 None（无需响应时）
        """
        # --- 步骤1: 处理系统消息（子Agent的回报结果） ---
        # 系统消息的 chat_id 格式为 "原始渠道:原始聊天ID"，用于路由回原始对话
        if msg.channel == "system":
            return await self._process_system_message(msg)
        
        # 日志记录（截取前80字符作为预览）
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")
        
        # --- 步骤2: 获取或创建会话 ---
        # session_key 格式通常为 "channel:chat_id"，用于唯一标识一个对话
        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        
        # --- 步骤3: 处理斜杠命令 ---
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            # /new: 开启新会话，先将当前对话归档到记忆中，再清空会话
            await self._consolidate_memory(session, archive_all=True)
            session.clear()
            self.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 New session started. Memory consolidated.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 nanobot commands:\n/new — Start a new conversation\n/help — Show available commands")
        
        # --- 步骤4: 记忆管理 —— 当会话消息过多时自动归档 ---
        # 类似于 Java 中的日志轮转（log rotation），防止上下文窗口溢出
        if len(session.messages) > self.memory_window:
            await self._consolidate_memory(session)
        
        # --- 步骤5: 更新工具上下文 ---
        # 某些工具需要知道当前消息来自哪个渠道和聊天，以便正确路由响应
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)
        
        # --- 步骤6: 构建 LLM 消息列表 ---
        # 将系统提示词 + 历史对话 + 当前消息组装成 LLM API 需要的格式
        messages = self.context.build_messages(
            history=session.get_history(),  # 获取 LLM 格式的历史消息
            current_message=msg.content,
            media=msg.media if msg.media else None,  # 多媒体附件（如图片）
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        
        # --- 步骤7: Agent ReAct 循环 ---
        # 这是核心的推理-行动循环，最多执行 max_iterations 次
        iteration = 0
        final_content = None
        tools_used: list[str] = []  # 记录使用过的工具名称
        
        while iteration < self.max_iterations:
            iteration += 1
            
            # 7a: 调用 LLM 进行推理
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),  # 传入可用工具的 JSON Schema 定义
                model=self.model
            )
            
            # 7b: 检查是否有工具调用请求
            if response.has_tool_calls:
                # 将 LLM 的助手消息（含工具调用）加入消息列表
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # 参数必须是 JSON 字符串
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,  # 思维链内容（DeepSeek-R1 等模型）
                )
                
                # 7c: 逐个执行工具调用
                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    # 将工具执行结果加入消息列表（LLM 会在下次迭代中看到）
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                # 7d: 交错思维链（Interleaved CoT）—— 让 LLM 在下次行动前先反思
                # 这是一种提升 Agent 质量的技巧，避免 LLM 盲目连续调用工具
                messages.append({"role": "user", "content": "Reflect on the results and decide next steps."})
            else:
                # 没有工具调用，说明 LLM 已经给出了最终回复
                final_content = response.content
                break
        
        # --- 步骤8: 处理最终响应 ---
        if final_content is None:
            if iteration >= self.max_iterations:
                final_content = f"Reached {self.max_iterations} iterations without completion."
            else:
                final_content = "I've completed processing but have no response to give."
        
        # 日志记录响应预览
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")
        
        # --- 步骤9: 保存会话历史 ---
        # 将用户消息和助手回复存入会话（附带使用的工具名称，便于后续记忆归档）
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content,
                            tools_used=tools_used if tools_used else None)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},  # 透传元数据（如 Slack 的 thread_ts 用于线程回复）
        )
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        处理系统消息（主要是子Agent的回报结果）。

        子Agent 完成后台任务后，会通过系统消息通道将结果发回。
        chat_id 字段编码了原始渠道信息，格式为 "原始渠道:原始聊天ID"，
        这样主Agent可以将结果路由回正确的对话中。

        处理流程与 _process_message 类似，但使用原始渠道的会话上下文。

        参数：
            msg: 系统入站消息（channel="system"）

        返回：
            路由回原始渠道的响应消息
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # 解析原始渠道信息（chat_id 格式："channel:chat_id"）
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]  # 原始渠道（如 "telegram"）
            origin_chat_id = parts[1]  # 原始聊天 ID
        else:
            # 降级处理
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # 使用原始会话的上下文（确保子Agent的结果在正确的对话上下文中处理）
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        
        # 更新工具上下文为原始渠道（确保后续工具调用发送到正确的聊天）
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)
        
        # 构建消息列表（使用原始会话的历史）
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        
        # Agent 循环（处理子Agent回报的结果）
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )
                
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                # 交错思维链：反思后再决定下一步
                messages.append({"role": "user", "content": "Reflect on the results and decide next steps."})
            else:
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "Background task completed."
        
        # 保存到原始会话（标记为系统消息）
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        # 返回到原始渠道
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """
        记忆合并归档 —— 将旧消息压缩为长期记忆和历史日志。

        当会话消息数量超过 memory_window 阈值时自动触发。
        使用 LLM 对旧消息进行智能摘要，分为两部分：
        1. history_entry: 追加到 HISTORY.md（可通过 grep 搜索的事件日志）
        2. memory_update: 更新 MEMORY.md（用户偏好、项目信息等长期记忆）

        【Java 类比】类似于日志轮转（LogRotation）+ Redis 缓存淘汰策略，
        既保留关键信息，又防止上下文窗口溢出。

        参数：
            session: 当前会话对象
            archive_all: 是否归档全部消息（True 用于 /new 命令）
        """
        if not session.messages:
            return
        memory = MemoryStore(self.workspace)
        if archive_all:
            old_messages = session.messages  # 归档所有消息
            keep_count = 0
        else:
            # 保留最近的一半消息（至少2条，最多10条）
            keep_count = min(10, max(2, self.memory_window // 2))
            old_messages = session.messages[:-keep_count]
        if not old_messages:
            return
        logger.info(f"Memory consolidation started: {len(session.messages)} messages, archiving {len(old_messages)}, keeping {keep_count}")

        # 将消息格式化为文本（包含时间戳、角色、使用的工具）
        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")
        conversation = "\n".join(lines)
        current_memory = memory.read_long_term()  # 读取当前长期记忆

        # 构建记忆合并提示词 —— 让 LLM 做智能摘要
        prompt = f"""You are a memory consolidation agent. Process this conversation and return a JSON object with exactly two keys:

1. "history_entry": A paragraph (2-5 sentences) summarizing the key events/decisions/topics. Start with a timestamp like [YYYY-MM-DD HH:MM]. Include enough detail to be useful when found by grep search later.

2. "memory_update": The updated long-term memory content. Add any new facts: user location, preferences, personal info, habits, project context, technical decisions, tools/services used. If nothing new, return the existing content unchanged.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{conversation}

Respond with ONLY valid JSON, no markdown fences."""

        try:
            # 调用 LLM 进行记忆摘要
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Respond only with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=self.model,
            )
            # 清理 LLM 返回的文本（可能包含 markdown 代码块标记）
            text = (response.content or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json.loads(text)  # 解析 JSON 结果

            # 追加历史日志条目
            if entry := result.get("history_entry"):
                memory.append_history(entry)
            # 更新长期记忆（仅在内容有变化时更新）
            if update := result.get("memory_update"):
                if update != current_memory:
                    memory.write_long_term(update)

            # 裁剪会话消息，只保留最新的 keep_count 条
            session.messages = session.messages[-keep_count:] if keep_count else []
            self.sessions.save(session)
            logger.info(f"Memory consolidation done, session trimmed to {len(session.messages)} messages")
        except Exception as e:
            logger.error(f"Memory consolidation failed: {e}")

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        直接处理消息（用于 CLI 命令行或定时任务场景）。

        跳过消息总线，直接构造 InboundMessage 并调用 _process_message。
        这是一个便捷方法，适用于不通过聊天渠道而直接调用 Agent 的场景。

        【Java 类比】类似于在 Controller 之外直接调用 Service 方法，
        绕过了 HTTP 层直接进入业务逻辑。

        参数：
            content: 用户消息内容
            session_key: 会话标识符（默认 "cli:direct"）
            channel: 来源渠道标识（默认 "cli"）
            chat_id: 聊天 ID（默认 "direct"）

        返回：
            Agent 的响应文本
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )
        
        response = await self._process_message(msg, session_key=session_key)
        return response.content if response else ""