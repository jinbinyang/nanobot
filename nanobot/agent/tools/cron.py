"""
定时任务工具模块 (agent/tools/cron.py)

模块职责：
    提供 CronTool 工具，允许 Agent 通过 LLM function call 来创建、查看、删除定时任务。
    支持三种调度方式：
      - every_seconds: 按固定间隔重复执行（如每30秒）
      - cron_expr: 按 cron 表达式调度（如 "0 9 * * *" 每天9点）
      - at: 在指定时间点一次性执行（如 "2026-02-12T10:30:00"）

在架构中的位置：
    CronTool 是一个具体的 Tool 实现，注册到 ToolRegistry 后供 Agent 调用。
    它内部依赖 CronService（cron/service.py）来实际管理定时任务。
    当定时任务触发时，CronService 会通过消息总线将消息发送到指定的聊天渠道。

使用场景举例：
    用户："每天早上9点提醒我看新闻"
    → LLM 调用 cron 工具，action="add", message="看新闻", cron_expr="0 9 * * *"
    → CronTool 调用 CronService.add_job() 创建定时任务

二开提示（语音多智能体）：
    可扩展此工具支持语音提醒（TTS），在 _add_job 中添加语音合成逻辑。
"""

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


class CronTool(Tool):
    """
    定时任务工具，允许 Agent 调度提醒和重复任务。

    支持三种操作：
      - add: 创建新的定时任务
      - list: 列出所有已调度的任务
      - remove: 按 ID 删除任务

    类比 Java: 类似于封装了 ScheduledExecutorService 的 REST Controller。
    """

    def __init__(self, cron_service: CronService):
        """
        初始化定时任务工具。

        参数:
            cron_service: 定时任务服务实例，负责实际的任务调度和管理
        """
        self._cron = cron_service
        # 当前会话上下文，用于确定任务触发时将消息发送到哪个渠道
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """
        设置当前会话上下文。

        Agent 循环在处理消息时会调用此方法，将当前的渠道和聊天ID传入，
        这样创建的定时任务就知道触发时应该把消息发到哪里。

        参数:
            channel: 消息渠道标识（如 "telegram", "discord"）
            chat_id: 聊天/用户 ID
        """
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule reminders and recurring tasks. Actions: add, list, remove."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform"
                },
                "message": {
                    "type": "string",
                    "description": "Reminder message (for add)"
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)"
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)"
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')"
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for remove)"
                }
            },
            "required": ["action"]
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        **kwargs: Any
    ) -> str:
        """
        执行定时任务操作。

        参数:
            action: 操作类型 ("add" | "list" | "remove")
            message: 提醒消息内容（add 时必填）
            every_seconds: 重复间隔秒数（add 时可选，三种调度方式之一）
            cron_expr: cron 表达式（add 时可选，三种调度方式之一）
            at: ISO 格式的时间点（add 时可选，三种调度方式之一）
            job_id: 任务 ID（remove 时必填）

        返回:
            str: 操作结果描述
        """
        # 根据 action 分发到对应的私有方法
        if action == "add":
            return self._add_job(message, every_seconds, cron_expr, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(self, message: str, every_seconds: int | None, cron_expr: str | None, at: str | None) -> str:
        """
        创建一个新的定时任务。

        根据传入的调度参数（三选一）构建不同类型的 CronSchedule：
          - every_seconds → 固定间隔重复执行
          - cron_expr → 按 cron 表达式调度
          - at → 一次性定时执行（执行后自动删除）
        """
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"

        # 根据参数构建调度计划
        delete_after = False  # 标记任务执行后是否自动删除
        if every_seconds:
            # 固定间隔调度，内部使用毫秒单位
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            # cron 表达式调度（如 "0 9 * * *" 表示每天早9点）
            schedule = CronSchedule(kind="cron", expr=cron_expr)
        elif at:
            # 一次性定时执行，将 ISO 时间转换为毫秒时间戳
            from datetime import datetime
            dt = datetime.fromisoformat(at)
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True  # 一次性任务执行后自动清理
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        # 调用 CronService 创建任务
        job = self._cron.add_job(
            name=message[:30],          # 任务名称取消息前30字符
            schedule=schedule,           # 调度计划
            message=message,             # 完整的提醒消息
            deliver=True,                # 标记需要投递消息
            channel=self._channel,       # 目标渠道
            to=self._chat_id,            # 目标聊天ID
            delete_after_run=delete_after,  # 是否执行后删除
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _list_jobs(self) -> str:
        """列出所有已调度的定时任务。"""
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        """按 ID 删除一个定时任务。"""
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"