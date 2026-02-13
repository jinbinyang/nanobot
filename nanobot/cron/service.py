"""定时任务调度服务 - 管理和执行 Agent 的定时任务。

本模块实现了完整的定时任务调度引擎：
- 任务持久化：JSON 文件存储（~/.nanobot/data/cron/jobs.json）
- 三种调度模式：定时触发（at）、固定间隔（every）、cron 表达式
- 异步定时器：基于 asyncio.Task 的精确定时触发
- 任务生命周期：增删改查、启用/禁用、手动触发
- 一次性任务：执行后自动禁用或删除

架构设计：
- 使用单一定时器模式：计算所有任务的最早触发时间，设置一个 asyncio sleep
- 定时器到期后检查所有到期任务并执行
- 执行通过 on_job 回调委托给外部（通常是 AgentLoop）

依赖：
- croniter（可选）：解析 cron 表达式（pip install croniter）

二开提示：
- 可扩展为支持多 Agent 任务调度（每个任务指定不同 Agent）
- 可添加任务优先级、并发控制等高级调度特性
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore


def _now_ms() -> int:
    """获取当前时间的毫秒级 Unix 时间戳。"""
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """根据调度规则计算下次执行时间。

    参数:
        schedule: 调度规则对象
        now_ms: 当前时间（毫秒时间戳）

    返回:
        下次执行的毫秒时间戳，无法计算时返回 None
    """
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None
    
    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # 从当前时间开始计算下一个间隔
        return now_ms + schedule.every_ms
    
    if schedule.kind == "cron" and schedule.expr:
        try:
            from croniter import croniter
            cron = croniter(schedule.expr, time.time())
            next_time = cron.get_next()
            return int(next_time * 1000)
        except Exception:
            return None
    
    return None


class CronService:
    """定时任务调度服务 - 管理和执行 Agent 的定时任务。

    职责：
    - 从磁盘加载/保存任务列表
    - 计算每个任务的下次执行时间
    - 通过异步定时器在到期时触发任务
    - 提供 CRUD API 供 CLI 和工具调用

    调度机制：
    - 采用"最近到期"策略：只设置一个定时器，指向最早到期的任务
    - 定时器到期后扫描所有到期任务并逐个执行
    - 执行完成后重新计算下次触发时间并重置定时器
    """
    
    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None
    ):
        """
        初始化定时任务服务。

        参数:
            store_path: 任务持久化文件路径（如 ~/.nanobot/data/cron/jobs.json）
            on_job: 任务执行回调函数，接收 CronJob 并返回 Agent 回复文本
        """
        self.store_path = store_path
        self.on_job = on_job  # 任务执行回调，返回 Agent 响应文本
        self._store: CronStore | None = None      # 内存中的任务存储
        self._timer_task: asyncio.Task | None = None  # 当前激活的定时器任务
        self._running = False                      # 服务运行状态
    
    def _load_store(self) -> CronStore:
        """从磁盘加载任务列表（懒加载，仅首次调用时读取文件）。

        JSON 文件使用 camelCase 命名（如 nextRunAtMs），
        加载时转换为 Python 的 snake_case 数据模型。

        返回:
            CronStore 实例（包含所有任务）
        """
        if self._store:
            return self._store
        
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text())
                jobs = []
                for j in data.get("jobs", []):
                    jobs.append(CronJob(
                        id=j["id"],
                        name=j["name"],
                        enabled=j.get("enabled", True),
                        schedule=CronSchedule(
                            kind=j["schedule"]["kind"],
                            at_ms=j["schedule"].get("atMs"),
                            every_ms=j["schedule"].get("everyMs"),
                            expr=j["schedule"].get("expr"),
                            tz=j["schedule"].get("tz"),
                        ),
                        payload=CronPayload(
                            kind=j["payload"].get("kind", "agent_turn"),
                            message=j["payload"].get("message", ""),
                            deliver=j["payload"].get("deliver", False),
                            channel=j["payload"].get("channel"),
                            to=j["payload"].get("to"),
                        ),
                        state=CronJobState(
                            next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                            last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                            last_status=j.get("state", {}).get("lastStatus"),
                            last_error=j.get("state", {}).get("lastError"),
                        ),
                        created_at_ms=j.get("createdAtMs", 0),
                        updated_at_ms=j.get("updatedAtMs", 0),
                        delete_after_run=j.get("deleteAfterRun", False),
                    ))
                self._store = CronStore(jobs=jobs)
            except Exception as e:
                logger.warning(f"Failed to load cron store: {e}")
                self._store = CronStore()
        else:
            self._store = CronStore()
        
        return self._store
    
    def _save_store(self) -> None:
        """将任务列表持久化到磁盘（JSON 格式）。自动创建父目录。"""
        if not self._store:
            return
        
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                }
                for j in self._store.jobs
            ]
        }
        
        self.store_path.write_text(json.dumps(data, indent=2))
    
    async def start(self) -> None:
        """启动定时任务服务。加载任务、重新计算执行时间、激活定时器。"""
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info(f"Cron service started with {len(self._store.jobs if self._store else [])} jobs")
    
    def stop(self) -> None:
        """停止定时任务服务并取消定时器。"""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
    
    def _recompute_next_runs(self) -> None:
        """重新计算所有已启用任务的下次执行时间。通常在服务启动时调用。"""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
    
    def _get_next_wake_ms(self) -> int | None:
        """获取所有任务中最早的下次执行时间（用于设置定时器唤醒点）。"""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs 
                 if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None
    
    def _arm_timer(self) -> None:
        """设置下一个定时器触发点。取消旧定时器，创建新的 asyncio 任务。"""
        if self._timer_task:
            self._timer_task.cancel()
        
        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return
        
        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000
        
        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()
        
        self._timer_task = asyncio.create_task(tick())
    
    async def _on_timer(self) -> None:
        """定时器到期回调 - 执行所有到期的任务。"""
        if not self._store:
            return
        
        now = _now_ms()
        due_jobs = [
            j for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
        ]
        
        for job in due_jobs:
            await self._execute_job(job)
        
        self._save_store()
        self._arm_timer()
    
    async def _execute_job(self, job: CronJob) -> None:
        """执行单个定时任务。

        流程：
        1. 调用 on_job 回调（通过 Agent 执行任务消息）
        2. 更新任务状态（成功/失败、执行时间）
        3. 处理一次性任务（at 模式）：自动删除或禁用
        4. 计算下次执行时间（every/cron 模式）

        参数:
            job: 要执行的定时任务
        """
        start_ms = _now_ms()
        logger.info(f"Cron: executing job '{job.name}' ({job.id})")
        
        try:
            response = None
            if self.on_job:
                response = await self.on_job(job)
            
            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info(f"Cron: job '{job.name}' completed")
            
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error(f"Cron: job '{job.name}' failed: {e}")
        
        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()
        
        # 处理一次性任务（at 模式）
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            # 计算下次执行时间
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
    
    # ========== 公开 API ==========
    
    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """列出所有任务（按下次执行时间排序）。

        参数:
            include_disabled: 是否包含已禁用的任务

        返回:
            排序后的任务列表
        """
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))
    
    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """添加新的定时任务。

        参数:
            name: 任务名称
            schedule: 调度规则
            message: 发送给 Agent 的消息
            deliver: 是否将 Agent 回复投递到渠道
            channel: 投递目标渠道
            to: 投递目标地址
            delete_after_run: 执行后是否自动删除

        返回:
            创建的 CronJob 实例
        """
        store = self._load_store()
        now = _now_ms()
        
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )
        
        store.jobs.append(job)
        self._save_store()
        self._arm_timer()
        
        logger.info(f"Cron: added job '{name}' ({job.id})")
        return job
    
    def remove_job(self, job_id: str) -> bool:
        """删除指定 ID 的任务。返回是否成功删除。"""
        store = self._load_store()
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        removed = len(store.jobs) < before
        
        if removed:
            self._save_store()
            self._arm_timer()
            logger.info(f"Cron: removed job {job_id}")
        
        return removed
    
    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """启用或禁用指定的任务。启用时重新计算下次执行时间。"""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None
    
    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """手动触发执行指定的任务。force=True 时可执行已禁用的任务。"""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False
    
    def status(self) -> dict:
        """获取服务状态摘要（运行状态、任务数、下次唤醒时间）。"""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
