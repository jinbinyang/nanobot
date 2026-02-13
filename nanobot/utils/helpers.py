"""
工具函数集合 - nanobot 项目全局通用的辅助函数。

本模块提供路径管理、字符串处理、时间戳等基础工具函数，
被项目中的多个模块引用。

函数分类：
- 路径管理：ensure_dir, get_data_path, get_workspace_path, get_sessions_path, get_skills_path
- 字符串工具：truncate_string, safe_filename
- 时间工具：timestamp
- 解析工具：parse_session_key
"""

from pathlib import Path
from datetime import datetime


def ensure_dir(path: Path) -> Path:
    """
    确保目录存在，不存在则递归创建。

    参数:
        path: 目标目录路径

    返回:
        创建后的目录路径（原样返回）
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """获取 nanobot 数据目录（~/.nanobot）。自动创建不存在的目录。"""
    return ensure_dir(Path.home() / ".nanobot")


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    获取工作空间路径。

    工作空间是 Agent 的工作根目录，存放指令文件（AGENTS.md）、
    记忆文件（memory/）、技能脚本（skills/）等。

    参数:
        workspace: 自定义工作空间路径。为 None 时使用默认路径 ~/.nanobot/workspace

    返回:
        展开并确保存在的工作空间路径
    """
    if workspace:
        path = Path(workspace).expanduser()
    else:
        path = Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def get_sessions_path() -> Path:
    """获取会话存储目录（~/.nanobot/sessions）。自动创建不存在的目录。"""
    return ensure_dir(get_data_path() / "sessions")


def get_skills_path(workspace: Path | None = None) -> Path:
    """获取技能脚本目录（workspace/skills）。自动创建不存在的目录。"""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "skills")


def timestamp() -> str:
    """获取当前时间的 ISO 8601 格式字符串。"""
    return datetime.now().isoformat()


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """
    截断字符串到指定最大长度，超出时添加后缀。

    参数:
        s: 原始字符串
        max_len: 最大长度（包含后缀），默认 100
        suffix: 截断后缀，默认 "..."

    返回:
        截断后的字符串
    """
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def safe_filename(name: str) -> str:
    """
    将字符串转换为安全的文件名（移除/替换不安全字符）。

    替换的不安全字符包括：< > : " / \ | ? *

    参数:
        name: 原始文件名

    返回:
        安全的文件名字符串
    """
    # 替换不安全字符为下划线
    unsafe = '<>:"/\\|?*'
    for char in unsafe:
        name = name.replace(char, "_")
    return name.strip()


def parse_session_key(key: str) -> tuple[str, str]:
    """
    解析会话键为渠道名和聊天 ID。

    会话键格式为 "channel:chat_id"，如 "telegram:123456"、"cli:direct"。

    参数:
        key: 会话键字符串

    返回:
        (channel, chat_id) 元组

    异常:
        ValueError: 键格式不正确（不包含冒号分隔符）
    """
    parts = key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid session key: {key}")
    return parts[0], parts[1]
