"""
会话管理器实现模块 - 对话历史的存储、检索和管理。

本模块包含两个核心类：
- Session：单个对话会话，维护消息列表和元数据
- SessionManager：会话管理器，负责会话的 CRUD 操作和磁盘持久化

【存储格式 - JSONL】
每个会话存储为一个 .jsonl 文件（JSON Lines 格式）：
- 第一行：元数据行（_type="metadata"），包含创建时间、更新时间等
- 后续行：每行一条消息的 JSON 对象，包含 role、content、timestamp

JSONL 格式的优点：
1. 追加写入友好（无需读取整个文件再写入）
2. 逐行解析，内存占用可控
3. 人类可读，方便调试

【存储路径】
会话文件存储在 ~/.nanobot/sessions/ 目录下（用户家目录），
文件名由 session_key 转换而来（将冒号替换为下划线）。

【Java 开发者类比】
- Session 类似于 Java Servlet 的 HttpSession
- SessionManager 类似于一个带有文件持久化的 ConcurrentHashMap
- JSONL 格式类似于 Apache Kafka 的日志段（log segment）
- _cache 是一个简单的内存缓存层（类似 Guava Cache）
"""

import json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from nanobot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    单个对话会话 - 存储某个渠道某个聊天窗口的完整对话历史。

    每个 Session 通过 key（格式为 "channel:chat_id"）唯一标识，
    内部维护一个消息列表，每条消息包含角色（role）、内容（content）
    和时间戳（timestamp）。

    属性:
        key: 会话唯一标识，格式为 "channel:chat_id"（如 "telegram:123456"）
        messages: 对话消息列表，每条消息是一个字典
        created_at: 会话创建时间
        updated_at: 会话最后更新时间（每次添加消息时自动更新）
        metadata: 会话级别的附加元数据（如用户偏好、会话标签等）
    """

    key: str                                                    # 会话唯一标识
    messages: list[dict[str, Any]] = field(default_factory=list)  # 对话消息列表
    created_at: datetime = field(default_factory=datetime.now)  # 创建时间
    updated_at: datetime = field(default_factory=datetime.now)  # 最后更新时间
    metadata: dict[str, Any] = field(default_factory=dict)      # 附加元数据

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """
        向会话中添加一条消息。

        参数:
            role: 消息角色（'user'、'assistant'、'system'、'tool'）
            content: 消息文本内容
            **kwargs: 额外的消息属性（如 tool_calls、name 等）
        """
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),  # ISO 8601 格式时间戳
            **kwargs  # 合并额外属性（如工具调用信息）
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()  # 更新会话的最后修改时间

    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """
        获取用于 LLM 上下文的消息历史。

        只返回最近的 max_messages 条消息，并且只保留 role 和 content 字段，
        去除 timestamp 等 LLM 不需要的元数据，减少 token 消耗。

        参数:
            max_messages: 最大返回消息数，默认 50 条

        返回:
            精简后的消息列表，每条消息只含 role 和 content
        """
        # 截取最近的消息（滑动窗口策略）
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages

        # 转换为 LLM 标准格式（只保留 role 和 content）
        return [{"role": m["role"], "content": m["content"]} for m in recent]

    def clear(self) -> None:
        """
        清空会话中的所有消息。

        保留会话本身（key 和 metadata 不变），仅清除对话历史。
        常用于用户主动要求"重新开始对话"的场景。
        """
        self.messages = []
        self.updated_at = datetime.now()


class SessionManager:
    """
    会话管理器 - 管理所有对话会话的 CRUD 操作。

    采用"内存缓存 + 磁盘持久化"的双层架构：
    - 内存层（_cache）：快速访问活跃会话，避免频繁磁盘 I/O
    - 磁盘层（JSONL 文件）：持久化存储，程序重启后可恢复

    会话文件存储在 ~/.nanobot/sessions/ 目录下。

    属性:
        workspace: 工作区根目录（当前未直接使用，保留用于未来扩展）
        sessions_dir: 会话文件存储目录
        _cache: 内存会话缓存字典 {session_key: Session}
    """

    def __init__(self, workspace: Path):
        """
        初始化会话管理器。

        参数:
            workspace: 工作区根目录路径
        """
        self.workspace = workspace
        # 会话存储在用户家目录下，而非工作区内，确保跨工作区共享
        self.sessions_dir = ensure_dir(Path.home() / ".nanobot" / "sessions")
        self._cache: dict[str, Session] = {}  # 内存缓存

    def _get_session_path(self, key: str) -> Path:
        """
        根据会话键获取对应的磁盘文件路径。

        将 "channel:chat_id" 中的冒号替换为下划线，
        并通过 safe_filename 确保文件名不含非法字符。

        参数:
            key: 会话键（如 "telegram:123456"）

        返回:
            会话文件的完整路径（如 ~/.nanobot/sessions/telegram_123456.jsonl）
        """
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        获取已有会话或创建新会话。

        查找顺序：内存缓存 → 磁盘文件 → 新建空会话。
        获取后自动放入缓存，后续访问直接从内存返回。

        参数:
            key: 会话键（通常为 "channel:chat_id"）

        返回:
            已存在的或新创建的 Session 对象
        """
        # 第一优先级：内存缓存（最快）
        if key in self._cache:
            return self._cache[key]

        # 第二优先级：从磁盘加载
        session = self._load(key)
        if session is None:
            # 第三优先级：创建全新的空会话
            session = Session(key=key)

        # 放入缓存供后续快速访问
        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """
        从磁盘加载会话。

        读取 JSONL 文件，逐行解析：
        - _type="metadata" 的行作为元数据
        - 其他行作为消息记录

        参数:
            key: 会话键

        返回:
            加载的 Session 对象，文件不存在或解析失败时返回 None
        """
        path = self._get_session_path(key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue  # 跳过空行

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        # 元数据行：提取会话级信息
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                    else:
                        # 消息行：追加到消息列表
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata
            )
        except Exception as e:
            # 文件损坏时优雅降级，记录警告但不中断程序
            logger.warning(f"Failed to load session {key}: {e}")
            return None

    def save(self, session: Session) -> None:
        """
        将会话持久化到磁盘。

        采用全量覆盖写入（非追加），确保文件内容与内存一致。
        写入顺序：元数据行 → 所有消息行。

        参数:
            session: 要保存的会话对象
        """
        path = self._get_session_path(session.key)

        with open(path, "w") as f:
            # 第一行：写入元数据
            metadata_line = {
                "_type": "metadata",
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata
            }
            f.write(json.dumps(metadata_line) + "\n")

            # 后续行：写入每条消息
            for msg in session.messages:
                f.write(json.dumps(msg) + "\n")

        # 更新缓存，保持缓存与磁盘一致
        self._cache[session.key] = session

    def delete(self, key: str) -> bool:
        """
        删除指定会话（同时清除缓存和磁盘文件）。

        参数:
            key: 会话键

        返回:
            True 表示成功删除，False 表示会话不存在
        """
        # 从内存缓存中移除
        self._cache.pop(key, None)

        # 从磁盘删除文件
        path = self._get_session_path(key)
        if path.exists():
            path.unlink()  # 删除文件（类似 Java 的 Files.delete）
            return True
        return False

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        列出所有已存储的会话信息。

        扫描会话目录下的所有 .jsonl 文件，读取每个文件的第一行（元数据行）
        获取会话的基本信息。返回结果按最后更新时间倒序排列。

        返回:
            会话信息字典列表，每个字典包含 key、created_at、updated_at、path
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # 只读取第一行（元数据行），避免加载整个会话文件
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            sessions.append({
                                "key": path.stem.replace("_", ":"),  # 文件名还原为 session_key
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue  # 跳过损坏的文件

        # 按最后更新时间倒序排列（最近活跃的会话排在前面）
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)