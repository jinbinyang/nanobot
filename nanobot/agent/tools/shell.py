"""
Shell 命令执行工具模块 (agent/tools/shell.py)

模块职责：
    提供 ExecTool 工具，允许 Agent 通过 LLM function call 执行 Shell 命令。
    这是 nanobot 最强大也最危险的工具——Agent 可以像人类一样在终端执行任意命令。

    内置多层安全防护机制：
      1. 危险命令黑名单（deny_patterns）：拦截 rm -rf、格式化磁盘、fork 炸弹等
      2. 命令白名单（allow_patterns）：可选，仅允许匹配的命令执行
      3. 工作区限制（restrict_to_workspace）：可选，禁止访问工作目录外的路径
      4. 超时保护（timeout）：命令超时自动终止，防止无限阻塞
      5. 输出截断（max_len=10000）：防止超长输出耗尽内存

在架构中的位置：
    ExecTool 注册到 ToolRegistry 后，Agent 可以执行 git、npm、pip、curl 等命令。
    这使得 Agent 具备了完整的开发者能力（构建、测试、部署等）。

设计模式对比（Java 视角）：
    类似于 Java 的 Runtime.exec() 或 ProcessBuilder，但包装了：
    - 安全检查层（_guard_command，类似 SecurityManager）
    - 超时控制（asyncio.wait_for，类似 Future.get(timeout)）
    - 输出格式化（stdout + stderr 合并，退出码附加）

二开提示（语音多智能体）：
    可通过此工具执行 ffmpeg 等音频处理命令，实现语音格式转换、音频剪辑等功能。
    注意：需要在 deny_patterns 中适当放行音频相关命令。
"""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class ExecTool(Tool):
    """
    Shell 命令执行工具。

    允许 Agent 在服务器上执行 Shell 命令，并返回标准输出和错误输出。
    内置安全防护机制，防止执行危险命令。

    类比 Java: 类似于 ProcessBuilder + SecurityManager 的组合。
    """

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
    ):
        """
        初始化 Shell 执行工具。

        参数:
            timeout: 命令最大执行时间（秒），超时后自动 kill 进程
            working_dir: 默认工作目录，None 则使用当前进程工作目录
            deny_patterns: 危险命令正则黑名单，匹配则拒绝执行
            allow_patterns: 命令正则白名单（可选），设置后只有匹配的命令才能执行
            restrict_to_workspace: 是否限制只能访问工作目录内的路径
        """
        self.timeout = timeout
        self.working_dir = working_dir
        # 默认的危险命令黑名单——拦截常见的破坏性操作
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr（递归删除）
            r"\bdel\s+/[fq]\b",              # Windows: del /f, del /q（强制删除）
            r"\brmdir\s+/s\b",               # Windows: rmdir /s（递归删除目录）
            r"\b(format|mkfs|diskpart)\b",   # 磁盘格式化操作
            r"\bdd\s+if=",                   # dd 命令（直接写磁盘，极其危险）
            r">\s*/dev/sd",                  # 重定向写入磁盘设备
            r"\b(shutdown|reboot|poweroff)\b",  # 系统关机/重启
            r":\(\)\s*\{.*\};\s*:",          # fork 炸弹 :(){ :|:& };:
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        """
        执行 Shell 命令。

        参数:
            command: 要执行的 Shell 命令字符串
            working_dir: 可选的工作目录（优先级：参数 > 实例默认 > 当前进程目录）

        返回:
            str: 命令输出（stdout + stderr），超长时截断

        执行流程:
            1. 确定工作目录
            2. 安全检查（_guard_command）
            3. 创建子进程异步执行
            4. 等待完成（带超时保护）
            5. 合并 stdout/stderr 并格式化返回
        """
        # 工作目录优先级：显式参数 > 实例默认值 > 当前进程工作目录
        cwd = working_dir or self.working_dir or os.getcwd()
        # 执行安全检查，不通过则直接返回错误
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        try:
            # 使用 asyncio 创建子进程（非阻塞），类似 Java 的 ProcessBuilder
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,  # 捕获标准输出
                stderr=asyncio.subprocess.PIPE,  # 捕获标准错误
                cwd=cwd,
            )

            try:
                # 带超时保护的等待进程完成
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                # 超时则强制终止进程
                process.kill()
                return f"Error: Command timed out after {self.timeout} seconds"

            # 组装输出结果
            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            # 非零退出码表示命令执行失败，附加退出码信息
            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # 截断超长输出，防止耗尽 LLM 上下文窗口
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """
        命令安全检查（尽最大努力防护，非绝对安全）。

        三层防护逻辑：
        1. 黑名单检查：匹配 deny_patterns 中的任何模式则拒绝
        2. 白名单检查：如果设置了 allow_patterns，不匹配则拒绝
        3. 路径遍历检查：如果启用了 restrict_to_workspace，检查命令中的路径

        参数:
            command: 待检查的命令字符串
            cwd: 当前工作目录

        返回:
            str | None: 如果命令不安全返回错误信息，安全则返回 None
        """
        cmd = command.strip()
        lower = cmd.lower()

        # 第一层：黑名单检查——逐个匹配危险命令模式
        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        # 第二层：白名单检查——如果设置了白名单，命令必须匹配至少一个模式
        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        # 第三层：工作区路径限制检查
        if self.restrict_to_workspace:
            # 检测路径遍历攻击（../）
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            # 提取命令中的 Windows 和 POSIX 风格绝对路径
            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            # 只匹配绝对路径，避免对相对路径（如 .venv/bin/python）的误报
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)

            # 检查所有提取到的绝对路径是否在工作目录内
            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                # 如果路径是绝对路径且不在工作目录内，则拒绝
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None  # 所有检查通过，命令安全