"""
心跳服务模块 - 定期唤醒 Agent 检查待办任务。

本模块提供 HeartbeatService，用于定时读取工作空间中的 HEARTBEAT.md 文件，
如果其中包含待办任务，则通过 Agent 执行。

二开提示：
- 可扩展为多 Agent 心跳检查（每个 Agent 有独立的 HEARTBEAT 文件）
- 心跳间隔和提示词可自定义，适合构建定期巡检、自动汇报等场景
"""

from nanobot.heartbeat.service import HeartbeatService

__all__ = ["HeartbeatService"]
