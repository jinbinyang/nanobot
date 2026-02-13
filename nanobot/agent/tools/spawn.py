"""
子代理生成工具模块 (agent/tools/spawn.py)

模块职责：
    提供 SpawnTool 工具，允许 Agent 通过 LLM function call 创建后台子代理。
    当 Agent 判断某个任务比较复杂或耗时，可以将其委托给子代理在后台异步执行，
    主 Agent 则可以继续与用户对话，不被阻塞。

在架构中的位置：
    SpawnTool 是 SubagentManager（agent/subagent.py）的薄封装层：
    - SpawnTool 负责定义工具接口（name/description/parameters），供 LLM 调用
    - SubagentManager 负责实际的子代理创建和执行逻辑

    调用链：
    LLM → tool_call("spawn", {task: "..."})
      → SpawnTool.execute()
        → SubagentManager.spawn()
          → asyncio.create_task() 后台运行

设计模式对比（Java 视角）：
    SpawnTool 是一个瘦 Controller（接收 LLM 请求），
    SubagentManager 是 Service 层（实际业务逻辑）。
    类似于 @RestController 调用 @Service 的分层模式。

二开提示（语音多智能体）：
    在多 Agent 系统中，spawn 工具可以改造为"路由工具"：
    管家 Agent 根据用户需求调用 spawn 创建特定领域的 Agent（天气、日程、搜索等）。
"""

from typing import Any, TYPE_CHECKING

from nanobot.agent.tools.base import Tool

# TYPE_CHECKING: 仅在类型检查时导入，避免循环导入问题
# 类似 Java 中用 @Lazy 或接口解耦循环依赖
if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """
    后台子代理生成工具。

    当 Agent 需要执行耗时任务时（如长时间的文件处理、复杂的网页爬取），
    可以通过此工具创建一个子代理在后台执行，主 Agent 继续与用户对话。

    子代理完成后会通过消息总线将结果通知回主 Agent。
    """

    def __init__(self, manager: "SubagentManager"):
        """
        初始化生成工具。

        参数:
            manager: 子代理管理器实例，负责子代理的创建和生命周期管理
        """
        self._manager = manager
        # 记录当前对话的渠道信息，子代理完成后将结果发回此渠道
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        """
        设置当前会话上下文（子代理结果的回报目标）。

        参数:
            channel: 当前渠道标识
            chat_id: 当前聊天 ID
        """
        self._origin_channel = channel
        self._origin_chat_id = chat_id

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """
        生成一个后台子代理来执行指定任务。

        参数:
            task: 任务描述，会作为子代理的初始 user message
            label: 可选的任务标签，用于日志和通知中的显示

        返回:
            str: 子代理启动成功的确认消息（包含任务ID）
        """
        # 委托给 SubagentManager 执行实际的子代理创建
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
        )