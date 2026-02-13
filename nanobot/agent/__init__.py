"""
Agent 核心模块 —— nanobot 的"大脑"。

本包包含 Agent 运行所需的全部核心组件：
- AgentLoop: Agent 主循环，负责接收消息 → 调用LLM → 执行工具 → 返回响应（类比 Java 中的 Controller + Service 层）
- ContextBuilder: 上下文构建器，将系统提示词、历史对话、记忆、技能拼装成 LLM 可理解的消息格式
- MemoryStore: 记忆存储，管理长期记忆（MEMORY.md）和历史日志（HISTORY.md）
- SkillsLoader: 技能加载器，从 workspace/skills/ 目录动态加载扩展能力

【二开提示】如果要实现多智能体系统，AgentLoop 是核心改造点——
可以在此基础上实现"管家Agent"路由到不同领域的子Agent。
"""

from nanobot.agent.loop import AgentLoop
from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader

# 对外暴露的公开 API，其他模块通过 from nanobot.agent import AgentLoop 等方式导入
__all__ = ["AgentLoop", "ContextBuilder", "MemoryStore", "SkillsLoader"]
