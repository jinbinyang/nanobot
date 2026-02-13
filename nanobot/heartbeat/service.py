"""
心跳服务实现 - 定期唤醒 Agent 检查待办任务。

本模块实现了周期性心跳机制：
- 按固定间隔（默认 30 分钟）唤醒 Agent
- Agent 读取工作空间中的 HEARTBEAT.md 文件
- 如果文件中包含待办任务则执行，否则跳过
- Agent 回复 HEARTBEAT_OK 表示"无事可做"

架构设计：
- 基于 asyncio.Task 的定期循环
- 通过 on_heartbeat 回调委托给外部（通常是 AgentLoop）执行
- 自动跳过空文件以避免不必要的 LLM 调用

二开提示：
- 可修改 HEARTBEAT_PROMPT 来自定义 Agent 的心跳行为
- 可扩展为检查多个文件或数据源
- trigger_now() 方法支持手动触发，适合调试
"

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

# 默认心跳间隔：30 分钟
DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60

# 心跳时发送给 Agent 的提示词
HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: HEARTBEAT_OK"""

# Agent 回复中表示"无事可做"的标记令牌
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


def _is_heartbeat_empty(content: str | None) -> bool:
    """
    检查 HEARTBEAT.md 是否包含可执行的内容。

    跳过的内容：空行、标题行、HTML 注释、空复选框。
    如果文件中只包含这些内容，则认为"无任务"。

    参数:
        content: 文件内容字符串

    返回:
        True 表示无任务（空或无可执行内容），False 表示有任务
    """
    if not content:
        return True
    
    # 需要跳过的行模式：空行、标题、HTML 注释、复选框（无论勾选与否）
    skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}
    
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--") or line in skip_patterns:
            continue
        return False  # 发现可执行内容
    
    return True


class HeartbeatService:
    """
    心跳服务 - 定期唤醒 Agent 检查待办任务。

    工作流程：
    1. 每隔 interval_s 秒触发一次心跳
    2. 读取工作空间中的 HEARTBEAT.md 文件
    3. 如果文件为空或不存在，跳过（不消耗 LLM 配额）
    4. 如果文件有内容，调用 on_heartbeat 回调让 Agent 执行
    5. Agent 回复 HEARTBEAT_OK 表示无需处理

    使用场景：
    - 用户在 HEARTBEAT.md 中写入待办事项，Agent 定期执行
    - 适合定期巡检、数据汇总、提醒等场景
    """
    
    def __init__(
        self,
        workspace: Path,
        on_heartbeat: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        enabled: bool = True,
    ):
        """
        初始化心跳服务。

        参数:
            workspace: 工作空间目录路径（HEARTBEAT.md 所在目录）
            on_heartbeat: 心跳执行回调，接收提示词字符串，返回 Agent 回复
            interval_s: 心跳间隔秒数，默认 1800（30 分钟）
            enabled: 是否启用心跳服务
        """
        self.workspace = workspace
        self.on_heartbeat = on_heartbeat       # 心跳执行回调（委托给 AgentLoop）
        self.interval_s = interval_s           # 心跳间隔（秒）
        self.enabled = enabled                 # 是否启用
        self._running = False                  # 运行状态标志
        self._task: asyncio.Task | None = None  # 心跳循环任务
    
    @property
    def heartbeat_file(self) -> Path:
        """HEARTBEAT.md 文件的完整路径。"""
        return self.workspace / "HEARTBEAT.md"
    
    def _read_heartbeat_file(self) -> str | None:
        """读取 HEARTBEAT.md 文件内容。文件不存在或读取失败时返回 None。"""
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text()
            except Exception:
                return None
        return None
    
    async def start(self) -> None:
        """启动心跳服务。如果 enabled=False 则直接返回不启动。"""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Heartbeat started (every {self.interval_s}s)")
    
    def stop(self) -> None:
        """停止心跳服务并取消循环任务。"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
    
    async def _run_loop(self) -> None:
        """心跳主循环。先等待一个间隔周期，再执行心跳检查，循环往复。"""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
    
    async def _tick(self) -> None:
        """
        执行一次心跳检查。

        流程：
        1. 读取 HEARTBEAT.md
        2. 如果为空则跳过
        3. 调用 on_heartbeat 回调让 Agent 处理
        4. 检查 Agent 回复是否为 HEARTBEAT_OK（无任务）
        """
        content = self._read_heartbeat_file()
        
        # 如果 HEARTBEAT.md 为空或不存在，跳过本次心跳
        if _is_heartbeat_empty(content):
            logger.debug("Heartbeat: no tasks (HEARTBEAT.md empty)")
            return
        
        logger.info("Heartbeat: checking for tasks...")
        
        if self.on_heartbeat:
            try:
                response = await self.on_heartbeat(HEARTBEAT_PROMPT)
                
                # 检查 Agent 是否回复"无事可做"
                if HEARTBEAT_OK_TOKEN.replace("_", "") in response.upper().replace("_", ""):
                    logger.info("Heartbeat: OK (no action needed)")
                else:
                    logger.info(f"Heartbeat: completed task")
                    
            except Exception as e:
                logger.error(f"Heartbeat execution failed: {e}")
    
    async def trigger_now(self) -> str | None:
        """手动触发一次心跳。返回 Agent 的回复文本。"""
        if self.on_heartbeat:
            return await self.on_heartbeat(HEARTBEAT_PROMPT)
        return None
