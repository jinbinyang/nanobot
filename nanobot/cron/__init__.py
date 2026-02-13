"""
定时任务模块 - 提供 Agent 定时任务的调度和管理功能。

本模块包含：
- CronService：定时任务调度服务，负责任务的增删改查和定时触发
- CronJob：定时任务数据模型
- CronSchedule：调度规则定义（支持 cron 表达式、固定间隔、定时触发）

二开提示：
- 定时任务通过 Agent 执行，可用于定期巡检、自动汇报等场景
- 支持将执行结果投递到指定渠道（如 Telegram、钉钉等）
"""

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronSchedule

__all__ = ["CronService", "CronJob", "CronSchedule"]
