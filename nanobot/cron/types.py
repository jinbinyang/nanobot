"""
定时任务类型定义 - 定义定时任务系统的所有数据模型。

本模块定义了定时任务系统的核心数据结构：
- CronSchedule：调度规则（支持三种模式：定时触发、固定间隔、cron 表达式）
- CronPayload：任务载荷（要执行的消息内容和投递配置）
- CronJobState：任务运行时状态（下次运行时间、上次运行结果等）
- CronJob：完整的定时任务定义
- CronStore：持久化存储结构

二开提示：
- 如需添加新的调度类型（如基于事件触发），可扩展 CronSchedule.kind
- 如需添加新的载荷类型（如执行脚本），可扩展 CronPayload.kind
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CronSchedule:
    """
    定时任务调度规则。

    支持三种调度模式：
    - "at"：在指定时间点执行一次（一次性任务）
    - "every"：按固定间隔重复执行
    - "cron"：使用标准 cron 表达式调度（如 "0 9 * * *" = 每天早上9点）
    """
    kind: Literal["at", "every", "cron"]  # 调度模式类型
    # "at" 模式：目标执行时间（毫秒级 Unix 时间戳）
    at_ms: int | None = None
    # "every" 模式：执行间隔（毫秒）
    every_ms: int | None = None
    # "cron" 模式：标准 cron 表达式（如 "0 9 * * *"）
    expr: str | None = None
    # 时区设置（用于 cron 表达式，如 "Asia/Shanghai"）
    tz: str | None = None


@dataclass
class CronPayload:
    """
    定时任务载荷 - 描述任务要做什么。

    属性:
        kind: 载荷类型（"agent_turn" = 通过 Agent 执行，"system_event" = 系统事件）
        message: 发送给 Agent 的消息内容
        deliver: 是否将 Agent 回复投递到指定渠道
        channel: 投递目标渠道名称（如 "telegram"、"whatsapp"）
        to: 投递目标地址（如手机号、chat_id）
    """
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    deliver: bool = False         # 是否将结果投递到渠道
    channel: str | None = None    # 目标渠道（如 "whatsapp"）
    to: str | None = None         # 目标地址（如手机号）


@dataclass
class CronJobState:
    """
    定时任务运行时状态。

    记录任务的执行历史和下次计划执行时间，
    用于调度器判断何时触发任务以及展示任务状态。
    """
    next_run_at_ms: int | None = None   # 下次计划执行时间（毫秒时间戳）
    last_run_at_ms: int | None = None   # 上次实际执行时间（毫秒时间戳）
    last_status: Literal["ok", "error", "skipped"] | None = None  # 上次执行结果
    last_error: str | None = None       # 上次执行错误信息（仅在 status="error" 时有值）


@dataclass
class CronJob:
    """
    完整的定时任务定义。

    包含任务标识、调度规则、执行载荷和运行状态。
    通过 CronService 管理生命周期（创建、启用/禁用、删除、执行）。
    """
    id: str                        # 任务唯一 ID（8 位 UUID 前缀）
    name: str                      # 任务名称（用户可读）
    enabled: bool = True           # 是否启用
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))  # 调度规则
    payload: CronPayload = field(default_factory=CronPayload)      # 执行载荷
    state: CronJobState = field(default_factory=CronJobState)      # 运行时状态
    created_at_ms: int = 0         # 创建时间（毫秒时间戳）
    updated_at_ms: int = 0         # 最后更新时间（毫秒时间戳）
    delete_after_run: bool = False  # 执行后是否自动删除（用于一次性任务）


@dataclass
class CronStore:
    """
    定时任务持久化存储结构。

    序列化为 JSON 文件保存在 ~/.nanobot/data/cron/jobs.json。
    version 字段用于未来的数据格式迁移。
    """
    version: int = 1                                      # 存储格式版本号
    jobs: list[CronJob] = field(default_factory=list)     # 所有定时任务列表
