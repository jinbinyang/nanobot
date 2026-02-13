"""
记忆系统模块 - 为 Agent 提供持久化记忆能力。

本模块实现了一个简单但实用的双层记忆架构：
- 长期记忆（MEMORY.md）：存储关键事实、用户偏好等需要长期保留的信息
- 历史日志（HISTORY.md）：存储可通过 grep 搜索的对话/操作日志

【架构定位】
记忆系统是 Agent 上下文构建的重要输入之一，在 context.py 中被调用，
将记忆内容注入到 system prompt 中，使 Agent 具备跨会话记忆能力。

【Java 开发者类比】
类似于一个简单的文件型 KV 存储，MEMORY.md 相当于一个持久化的配置文件，
HISTORY.md 相当于一个追加写入的日志文件（类似 Log4j 的 FileAppender）。

【二开提示】
如果要实现多 Agent 系统，可以为每个 Agent 分配独立的记忆目录，
或者设计一个共享记忆层让管家 Agent 和领域 Agent 之间共享关键信息。
"""

from pathlib import Path

from nanobot.utils.helpers import ensure_dir


class MemoryStore:
    """
    双层记忆存储器。

    提供两种记忆机制：
    - MEMORY.md：长期事实记忆，由 Agent 通过 memory 技能主动读写，
      内容通常是用户偏好、关键事实等结构化信息
    - HISTORY.md：历史日志，采用追加写入模式，
      Agent 可以通过 grep 工具搜索历史记录

    属性:
        memory_dir: 记忆文件所在目录（workspace/memory/）
        memory_file: 长期记忆文件路径
        history_file: 历史日志文件路径
    """

    def __init__(self, workspace: Path):
        """
        初始化记忆存储器。

        参数:
            workspace: 工作区根目录路径，记忆文件将存储在 workspace/memory/ 下
        """
        # 确保记忆目录存在，不存在则自动创建
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        """
        读取长期记忆内容。

        返回:
            MEMORY.md 文件的完整文本内容，文件不存在时返回空字符串
        """
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        """
        覆盖写入长期记忆内容。

        注意：这是全量覆盖写入，不是追加。Agent 每次更新记忆时
        会先读取现有内容，修改后再整体写回。

        参数:
            content: 要写入的完整记忆内容
        """
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        """
        向历史日志追加一条记录。

        以追加模式写入，每条记录之间用空行分隔，方便后续 grep 搜索。

        参数:
            entry: 要追加的日志条目文本
        """
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")  # 去除尾部空白后追加双换行作为分隔

    def get_memory_context(self) -> str:
        """
        获取用于注入 Agent 上下文的记忆内容。

        该方法在每次 Agent 循环开始时被 context.py 调用，
        将长期记忆格式化为 Markdown 段落注入到 system prompt 中。

        返回:
            格式化的记忆上下文字符串，无记忆时返回空字符串
        """
        long_term = self.read_long_term()
        # 只有存在记忆内容时才返回格式化文本，避免注入空白段落
        return f"## Long-term Memory\n{long_term}" if long_term else ""