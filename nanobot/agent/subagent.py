"""
子代理管理模块 - 实现后台子代理的生命周期管理和任务执行。

本模块是 nanobot 多任务能力的核心，允许主 Agent 将耗时任务委托给
独立的子代理（Subagent）在后台异步执行，执行完毕后通过消息总线
将结果通知回主 Agent。

【子代理的工作原理】
1. 主 Agent 通过 spawn 工具创建子代理
2. 子代理获得独立的对话上下文（独立的 system prompt 和 messages 列表）
3. 子代理拥有文件读写、Shell 执行、Web 搜索等工具，但不能发消息和嵌套生成子代理
4. 子代理执行完毕后，通过消息总线将结果以 InboundMessage 形式发送回主 Agent
5. 主 Agent 收到后，将结果自然地转述给用户

【Java 开发者类比】
子代理类似于 Java 中的 CompletableFuture + 线程池模式：
- spawn() 相当于 executorService.submit()
- _run_subagent() 相当于 Callable 的 call() 方法
- _announce_result() 相当于 Future 完成后的回调（thenAccept）
- asyncio.Task 相当于 Future 对象

【二开提示】
当前的子代理是"任务型"的一次性执行器。在你的多 Agent 系统中，
可以扩展为"常驻型"领域 Agent：每个 Agent 有自己的技能集和记忆，
通过消息总线与管家 Agent 通信。核心改造点：
1. 给子代理增加独立的 MemoryStore
2. 给子代理配置专属技能（skills）
3. 将一次性任务改为持续监听模式
"""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool


class SubagentManager:
    """
    子代理管理器 - 负责子代理的创建、执行和结果回报。

    每个子代理都是一个异步任务（asyncio.Task），拥有：
    - 独立的 LLM 对话上下文（不共享主 Agent 的对话历史）
    - 聚焦的系统提示词（只关注分配的任务）
    - 受限的工具集（文件操作 + Shell + Web，无消息/嵌套生成能力）
    - 最多 15 轮迭代的执行限制（防止无限循环）

    属性:
        provider: LLM 提供者，与主 Agent 共享
        workspace: 工作区根目录
        bus: 消息总线，用于将结果发送回主 Agent
        model: 使用的 LLM 模型名称
        brave_api_key: Brave Search API 密钥（可选）
        exec_config: Shell 执行工具的配置
        restrict_to_workspace: 是否限制文件操作只在工作区内
        _running_tasks: 当前正在运行的子代理任务字典 {task_id: asyncio.Task}
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
    ):
        """
        初始化子代理管理器。

        参数:
            provider: LLM 调用提供者（与主 Agent 共享同一实例）
            workspace: 工作区根目录
            bus: 消息总线实例
            model: 指定模型名称，None 时使用 provider 的默认模型
            brave_api_key: Brave Search API 密钥
            exec_config: Shell 执行工具配置
            restrict_to_workspace: 是否限制文件/命令操作只在工作区范围内
        """
        # 延迟导入避免循环依赖
        from nanobot.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()  # 无指定时使用默认模型
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()  # 无配置时使用默认配置
        self.restrict_to_workspace = restrict_to_workspace
        self._running_tasks: dict[str, asyncio.Task[None]] = {}  # 追踪所有运行中的子代理

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
    ) -> str:
        """
        生成一个子代理来后台执行任务。

        这是主 Agent 调用的入口方法，创建异步任务后立即返回，
        不会阻塞主 Agent 的对话循环。

        参数:
            task: 任务描述文本，会作为子代理的 user message
            label: 可选的可读标签，用于日志和通知
            origin_channel: 发起请求的渠道名称（如 'telegram'）
            origin_chat_id: 发起请求的聊天 ID

        返回:
            状态消息，告知用户子代理已启动
        """
        task_id = str(uuid.uuid4())[:8]  # 生成8位短ID，方便日志追踪
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        # 记录结果应回报到哪个渠道/聊天
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
        }

        # 创建异步后台任务（类似 Java 的 executor.submit）
        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin)
        )
        self._running_tasks[task_id] = bg_task

        # 任务完成时自动从追踪字典中移除（类似 Future 的 cleanup 回调）
        bg_task.add_done_callback(lambda _: self._running_tasks.pop(task_id, None))

        logger.info(f"Spawned subagent [{task_id}]: {display_label}")
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
    ) -> None:
        """
        执行子代理的核心逻辑（在后台异步运行）。

        实现了一个简化版的 Agent 循环：
        1. 注册工具集（受限）
        2. 构建 system prompt 和初始 messages
        3. 循环调用 LLM，执行工具调用，直到 LLM 返回最终文本回复
        4. 将结果通过消息总线通知回主 Agent

        参数:
            task_id: 子代理唯一标识
            task: 任务描述
            label: 显示标签
            origin: 结果回报的目标渠道信息
        """
        logger.info(f"Subagent [{task_id}] starting task: {label}")

        try:
            # ===== 第一步：注册子代理可用的工具集 =====
            tools = ToolRegistry()
            allowed_dir = self.workspace if self.restrict_to_workspace else None

            # 文件操作工具（读/写/编辑/列目录）
            tools.register(ReadFileTool(allowed_dir=allowed_dir))
            tools.register(WriteFileTool(allowed_dir=allowed_dir))
            tools.register(EditFileTool(allowed_dir=allowed_dir))
            tools.register(ListDirTool(allowed_dir=allowed_dir))

            # Shell 执行工具
            tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
            ))

            # Web 工具（搜索 + 抓取）
            tools.register(WebSearchTool(api_key=self.brave_api_key))
            tools.register(WebFetchTool())
            # 注意：不注册 MessageTool（不能直接发消息给用户）
            # 注意：不注册 SpawnTool（不能嵌套创建子代理）

            # ===== 第二步：构建对话消息列表 =====
            system_prompt = self._build_subagent_prompt(task)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # ===== 第三步：执行简化版 Agent 循环 =====
            max_iterations = 15  # 最多15轮，防止无限循环
            iteration = 0
            final_result: str | None = None

            while iteration < max_iterations:
                iteration += 1

                # 调用 LLM 获取响应
                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=self.model,
                )

                if response.has_tool_calls:
                    # LLM 请求调用工具 → 执行工具并继续循环
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in response.tool_calls
                    ]
                    # 将 assistant 的工具调用消息加入对话历史
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })

                    # 逐个执行工具调用，将结果加入对话历史
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments)
                        logger.debug(f"Subagent [{task_id}] executing: {tool_call.name} with arguments: {args_str}")
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })
                else:
                    # LLM 返回纯文本 → 任务完成，退出循环
                    final_result = response.content
                    break

            if final_result is None:
                final_result = "Task completed but no final response was generated."

            logger.info(f"Subagent [{task_id}] completed successfully")
            await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except Exception as e:
            # 捕获所有异常，确保错误也能通知回主 Agent
            error_msg = f"Error: {str(e)}"
            logger.error(f"Subagent [{task_id}] failed: {e}")
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """
        通过消息总线将子代理的执行结果通知回主 Agent。

        将结果包装成一个 InboundMessage 发布到消息总线，
        主 Agent 的消息循环会接收到这条消息并自然地转述给用户。

        参数:
            task_id: 子代理 ID
            label: 任务标签
            task: 原始任务描述
            result: 执行结果文本
            origin: 原始渠道信息（决定结果发往哪个聊天）
            status: 执行状态，"ok" 或 "error"
        """
        status_text = "completed successfully" if status == "ok" else "failed"

        # 构造通知内容，指示主 Agent 如何向用户转述
        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # 构造内部消息，channel="system" 表示这是系统内部消息
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            # chat_id 格式为 "原渠道:原聊天ID"，用于路由到正确的会话
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
        )

        # 发布到消息总线，触发主 Agent 处理
        await self.bus.publish_inbound(msg)
        logger.debug(f"Subagent [{task_id}] announced result to {origin['channel']}:{origin['chat_id']}")

    def _build_subagent_prompt(self, task: str) -> str:
        """
        为子代理构建专用的系统提示词。

        子代理的提示词比主 Agent 简洁得多，只包含：
        - 当前时间信息
        - 角色定义（子代理身份）
        - 行为规则（专注任务、不发消息）
        - 能力范围说明
        - 工作区路径

        参数:
            task: 任务描述（当前未直接使用，但保留参数以便后续定制）

        返回:
            格式化的系统提示词字符串
        """
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"

        return f"""# Subagent

## Current Time
{now} ({tz})

You are a subagent spawned by the main agent to complete a specific task.

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}
Skills are available at: {self.workspace}/skills/ (read SKILL.md files as needed)

When you have completed the task, provide a clear summary of your findings or actions."""

    def get_running_count(self) -> int:
        """
        获取当前正在运行的子代理数量。

        返回:
            运行中的子代理数量
        """
        return len(self._running_tasks)