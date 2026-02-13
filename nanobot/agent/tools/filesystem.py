"""
文件系统工具模块 (agent/tools/filesystem.py)

模块职责：
    提供4个文件系统相关的工具，允许 Agent 通过 LLM function call 来操作本地文件：
      - ReadFileTool: 读取文件内容
      - WriteFileTool: 写入文件内容（自动创建父目录）
      - EditFileTool: 精确替换文件中的文本片段
      - ListDirTool: 列出目录内容

    所有工具都支持可选的目录限制（allowed_dir），防止 Agent 越权访问敏感路径。

在架构中的位置：
    这些工具注册到 ToolRegistry 后，Agent 可以像一个开发者一样操作文件系统。
    这是 nanobot 实现"编程助手"能力的基础——能读、写、编辑代码文件。

安全设计：
    _resolve_path() 辅助函数会：
    1. 展开 ~ 为用户主目录
    2. 解析为绝对路径（消除 ../ 等相对路径攻击）
    3. 检查路径是否在允许的目录范围内

设计模式对比（Java 视角）：
    类似于 Java NIO 的 Files 工具类，但封装为独立的 Tool 对象。
    allowed_dir 机制类似于 Java SecurityManager 的文件访问控制。

二开提示（语音多智能体）：
    如需让 Agent 处理语音文件（如保存录音、读取音频），可复用这些工具，
    或新增 AudioFileTool 支持音频格式的读写。
"""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


def _resolve_path(path: str, allowed_dir: Path | None = None) -> Path:
    """
    解析并校验文件路径。

    处理流程：
    1. 将字符串路径转为 Path 对象
    2. expanduser(): 展开 ~ 为用户主目录（如 ~/file → /home/user/file）
    3. resolve(): 解析为绝对路径（消除 ../ 等相对路径）
    4. 如果设置了 allowed_dir，检查路径是否在允许范围内

    参数:
        path: 原始路径字符串
        allowed_dir: 可选的允许目录限制

    返回:
        Path: 解析后的绝对路径

    异常:
        PermissionError: 路径超出允许目录范围
    """
    resolved = Path(path).expanduser().resolve()
    if allowed_dir and not str(resolved).startswith(str(allowed_dir.resolve())):
        raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


class ReadFileTool(Tool):
    """
    文件读取工具。

    读取指定路径文件的全部文本内容并返回。
    类比 Java: 类似于 Files.readString(Path)。
    """

    def __init__(self, allowed_dir: Path | None = None):
        """
        参数:
            allowed_dir: 可选的目录限制，设置后只能读取该目录下的文件
        """
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at the given path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        """
        读取文件内容。

        参数:
            path: 文件路径

        返回:
            str: 文件文本内容，或错误信息
        """
        try:
            file_path = _resolve_path(path, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            # 以 UTF-8 编码读取全部文本内容
            content = file_path.read_text(encoding="utf-8")
            return content
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


class WriteFileTool(Tool):
    """
    文件写入工具。

    将内容写入指定路径的文件，如果父目录不存在会自动创建。
    类比 Java: 类似于 Files.writeString(Path, content) + Files.createDirectories()。
    """

    def __init__(self, allowed_dir: Path | None = None):
        """
        参数:
            allowed_dir: 可选的目录限制，设置后只能写入该目录下的文件
        """
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write"
                }
            },
            "required": ["path", "content"]
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        """
        写入文件内容。

        参数:
            path: 目标文件路径
            content: 要写入的文本内容

        返回:
            str: 成功信息（含写入字节数），或错误信息
        """
        try:
            file_path = _resolve_path(path, self._allowed_dir)
            # 自动创建父目录（类似 Java 的 Files.createDirectories）
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """
    文件编辑工具（精确文本替换）。

    通过"查找旧文本 → 替换为新文本"的方式编辑文件。
    要求旧文本在文件中精确存在且唯一（出现多次会拒绝，避免误改）。

    类比 Java: 类似于 String.replace()，但附加了唯一性校验。
    这种设计确保了编辑的精确性和安全性。
    """

    def __init__(self, allowed_dir: Path | None = None):
        """
        参数:
            allowed_dir: 可选的目录限制
        """
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing old_text with new_text. The old_text must exist exactly in the file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace"
                },
                "new_text": {
                    "type": "string",
                    "description": "The text to replace with"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        """
        编辑文件：查找并替换文本。

        参数:
            path: 文件路径
            old_text: 要查找的原始文本（必须精确匹配）
            new_text: 替换后的新文本

        返回:
            str: 成功信息或错误信息

        安全机制：
            - 旧文本不存在 → 报错
            - 旧文本出现多次 → 警告，要求提供更多上下文使其唯一
            - 只替换第一次出现（replace count=1）
        """
        try:
            file_path = _resolve_path(path, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return f"Error: old_text not found in file. Make sure it matches exactly."

            # 检查旧文本出现次数，多次出现时拒绝替换以避免误操作
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            # 只替换第一次出现（虽然上面已确认唯一，但 replace(,1) 更安全）
            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")

            return f"Successfully edited {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """
    目录列表工具。

    列出指定目录下的所有文件和子目录，以 emoji 图标区分类型。
    类比 Java: 类似于 Files.list(Path) + 格式化输出。
    """

    def __init__(self, allowed_dir: Path | None = None):
        """
        参数:
            allowed_dir: 可选的目录限制
        """
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        """
        列出目录内容。

        参数:
            path: 目录路径

        返回:
            str: 格式化的目录列表（📁 表示目录，📄 表示文件），或错误信息
        """
        try:
            dir_path = _resolve_path(path, self._allowed_dir)
            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            # sorted() 按文件名排序，iterdir() 遍历目录内容
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"