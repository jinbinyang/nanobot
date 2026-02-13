"""
Agent 工具子包 (agent/tools)

模块职责：
    定义 Agent 可调用的所有"工具"（Tool），是 Agent 与外部世界交互的桥梁。
    工具系统采用经典的"注册表模式"：
      - Tool（基类）：定义工具的统一接口（名称、描述、参数 schema、执行方法）
      - ToolRegistry（注册表）：管理所有工具实例，提供按名称查找和执行的能力

在架构中的位置：
    Agent 核心循环 (agent/loop.py) 调用 LLM 后，LLM 可能返回 tool_calls，
    此时循环会通过 ToolRegistry.execute() 分发到具体的 Tool 实现。

内置工具清单：
    - ReadFileTool / WriteFileTool / EditFileTool / ListDirTool：文件系统操作
    - ExecTool：Shell 命令执行（带安全防护）
    - WebSearchTool / WebFetchTool：网页搜索和内容抓取
    - MessageTool：向用户发送消息
    - CronTool：定时任务调度
    - SpawnTool：创建后台子代理

二开提示（语音多智能体）：
    如需扩展新工具（如 TTS 语音合成、ASR 语音识别），
    只需继承 Tool 基类并注册到 ToolRegistry 即可。
"""

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry

__all__ = ["Tool", "ToolRegistry"]
