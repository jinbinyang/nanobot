"""
上下文构建器模块 —— 负责组装 Agent 的提示词和消息列表。

本模块是 Agent 与 LLM 之间的"翻译层"，将分散在各处的信息
（引导文件、记忆、技能、历史对话、当前消息）拼装成 LLM API 所需的标准消息格式。

核心概念（对 Java 开发者的说明）：
- LLM 的输入是一个消息列表（List<Message>），每条消息有 role 和 content
  - role="system": 系统提示词，定义 Agent 的身份和能力（类似 Java 中的配置文件）
  - role="user": 用户消息
  - role="assistant": AI 助手的回复
  - role="tool": 工具调用的执行结果
- 系统提示词由多个部分拼接而成：
  1. 核心身份（Identity）—— Agent 是谁、能做什么
  2. 引导文件（Bootstrap Files）—— workspace 下的 AGENTS.md, SOUL.md 等自定义文件
  3. 记忆（Memory）—— 长期记忆 MEMORY.md 的内容
  4. 技能（Skills）—— 从 skills/ 目录加载的扩展能力描述

【二开提示】
要实现多智能体系统，可以在 build_system_prompt() 中根据 Agent 角色
动态切换不同的身份描述和技能集合。
"""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore  # 记忆存储
from nanobot.agent.skills import SkillsLoader  # 技能加载器


class ContextBuilder:
    """
    上下文构建器 —— 将系统提示词、历史对话、技能和记忆组装成 LLM 消息格式。

    核心职责：
    1. 从 workspace 加载引导文件（AGENTS.md, SOUL.md 等）作为 Agent 行为
    2. 从记忆存储中加载长期记忆
    3. 从技能目录加载可用技能
    4. 将以上内容拼接成系统提示词
    5. 结合历史对话和当前消息，构建完整的 LLM 输入

    【Java 类比】类似于一个 PromptTemplateService，
    负责将模板 + 变量渲染成最终的提示词字符串。

    属性：
        workspace: 工作区路径
        memory: 记忆存储实例
        skills: 技能加载器实例
        BOOTSTRAP_FILES: 引导文件名列表，按顺序加载
    """
    
    # 引导文件列表 —— 这些文件放在 workspace 根目录下，用于自定义 Agent 行为
    # AGENTS.md: 多Agent配置  SOUL.md: Agent人格  USER.md: 用户信息
    # TOOLS.md: 工具使用指南  IDENTITY.md: 身份定义
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    
    def __init__(self, workspace: Path):
        """
        初始化上下文构建器。

        参数：
            workspace: 工作区目录路径，所有引导文件、记忆、技能都从此目录加载
        """
        self.workspace = workspace
        self.memory = MemoryStore(workspace)  # 记忆存储（管理 MEMORY.md 和 HISTORY.md）
        self.skills = SkillsLoader(workspace)  # 技能加载器（扫描 skills/ 目录）
    
    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """
        构建系统提示词 —— Agent 的"大脑配置"。

        系统提示词决定了 Agent 的身份、知识和能力范围。
        由以下部分按顺序拼接（用 "---" 分隔）：
        1. 核心身份 —— Agent 名称、能力描述、当前时间等
        2. 引导文件 —— workspace 下的自定义配置文件
        3. 记忆上下文 —— 长期记忆中的用户偏好等
        4. 始终加载的技能 —— 标记为 always 的技能全文
        5. 可用技能摘要 —— 其他技能的简要描述（Agent 需要时自行读取）

        参数：
            skill_names: 指定要加载的技能名称列表（可选）

        返回：
            完整的系统提示词字符串
        """
        parts = []
        
        # 1. 核心身份（包含当前时间、运行环境、工作区路径等动态信息）
        parts.append(self._get_identity())
        
        # 2. 引导文件（用户自定义的 Agent 配置）
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)
        
        # 3. 记忆上下文（长期记忆 MEMORY.md 的内容）
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")
        
        # 4. 始终加载的技能（always=true 的技能，全文注入上下文）
        # 这种技能会占用上下文窗口，但 Agent 随时可用
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")
        
        # 5. 可用技能摘要（仅显示名称和描述，Agent 需要时用 read_file 加载全文）
        # 这是一种"渐进式加载"策略，节省上下文窗口
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")
        
        # 用 "---" 分隔各部分（Markdown 水平线）
        return "\n\n---\n\n".join(parts)
    
    def _get_identity(self) -> str:
        """
        生成核心身份描述 —— Agent 的"自我认知"。

        包含以下动态信息：
        - 当前日期时间和时区
        - 运行环境（操作系统、CPU架构、Python版本）
        - 工作区路径
        - 记忆文件和技能文件的位置
        - Agent 的行为指南

        返回：
            格式化的身份描述字符串
        """
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")  # 如 "2024-01-15 14:30 (Monday)"
        tz = _time.strftime("%Z") or "UTC"  # 时区名称
        workspace_path = str(self.workspace.expanduser().resolve())  # 展开 ~ 为完整路径
        system = platform.system()  # 操作系统名称
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        
        return f"""# nanobot 🐱

You are nanobot, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks

## Current Time
{now} ({tz})

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable)
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel (like WhatsApp).
For normal conversation, just respond with text - do not call the message tool.

Always be helpful, accurate, and concise. When using tools, think step by step: what you know, what you need, and why you chose this tool.
When remembering something important, write to {workspace_path}/memory/MEMORY.md
To recall past events, grep {workspace_path}/memory/HISTORY.md"""
    
    def _load_bootstrap_files(self) -> str:
        """
        加载所有引导文件。

        从 workspace 根目录按顺序读取 BOOTSTRAP_FILES 列表中的文件。
        不存在的文件会被跳过（不报错）。

        返回：
            所有引导文件内容拼接的字符串，空字符串表示没有找到任何引导文件
        """
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        构建完整的 LLM 消息列表 —— Agent 每次调用 LLM 的入口。

        消息列表的结构：
        1. [系统提示词] — 由 build_system_prompt() 生成
        2. [历史消息...]  — 之前的对话记录
        3. [当前用户消息] — 本次用户输入（可能附带图片等多媒体）

        【Java 类比】类似于构建一个 HTTP 请求体（RequestBody），
        将 header（系统提示词）+ body（历史+当前消息）组装在一起。

        参数：
            history: 历史对话消息列表（LLM 格式：[{role, content}, ...]）
            current_message: 当前用户消息文本
            skill_names: 要加载的技能名称列表（可选）
            media: 多媒体文件路径列表（如图片路径，用于视觉理解）
            channel: 当前渠道名称（如 "telegram"）
            chat_id: 当前聊天 ID

        返回：
            完整的 LLM 消息列表，可直接传给 provider.chat()
        """
        messages = []

        # 1. 系统提示词（定义 Agent 身份和能力）
        system_prompt = self.build_system_prompt(skill_names)
        # 附加当前会话信息（让 Agent 知道自己在和哪个渠道的哪个用户对话）
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # 2. 历史对话
        messages.extend(history)

        # 3. 当前用户消息（可能包含图片附件）
        user_content = self._build_user_content(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """
        构建用户消息内容（支持图片等多媒体附件）。

        如果没有附件，直接返回文本字符串。
        如果有图片附件，返回多模态内容列表（OpenAI Vision API 格式）：
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
         {"type": "text", "text": "用户消息"}]

        参数：
            text: 用户消息文本
            media: 多媒体文件路径列表（可选）

        返回：
            纯文本字符串 或 多模态内容列表
        """
        if not media:
            return text  # 无附件，直接返回文本
        
        # 处理图片附件：读取文件并转为 base64 编码
        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)  # 推断 MIME 类型（如 "image/png"）
            # 跳过非图片文件或不存在的文件
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            # 将图片读取为 base64 字符串（类似 Java 的 Base64.getEncoder().encodeToString()）
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        
        if not images:
            return text  # 没有有效图片，退回纯文本
        # 返回多模态格式：图片在前，文本在后
        return images + [{"type": "text", "text": text}]
    
    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str
    ) -> list[dict[str, Any]]:
        """
        将工具执行结果添加到消息列表中。

        在 Agent 循环中，LLM 请求调用工具后，工具执行完毕，
        需要将结果以 role="tool" 的消息格式反馈给 LLM。

        参数：
            messages: 当前消息列表
            tool_call_id: 工具调用的唯一 ID（由 LLM 生成，用于匹配请求和结果）
            tool_name: 工具名称
            result: 工具执行结果字符串

        返回：
            追加了工具结果的消息列表
        """
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages
    
    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        将助手消息添加到消息列表中。

        在 Agent 循环中，LLM 的每次响应都需要作为 role="assistant" 的消息
        加入列表，以便后续调用时 LLM 能看到自己之前的输出。

        特殊处理：
        - tool_calls: 如果 LLM 请求了工具调用，需要在消息中附带工具调用信息
        - reasoning_content: 部分模型（如 DeepSeek-R1、Kimi）会输出思维链内容，
          必须保留在消息中，否则后续调用时模型会报错

        参数：
            messages: 当前消息列表
            content: 助手回复内容
            tool_calls: 工具调用列表（可选）
            reasoning_content: 思维链/推理内容（可选，仅部分模型支持）

        返回：
            追加了助手消息的消息列表
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        
        if tool_calls:
            msg["tool_calls"] = tool_calls
        
        # 思维链模型（如 DeepSeek-R1）要求历史消息中保留 reasoning_content，
        # 否则会拒绝处理后续消息
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        
        messages.append(msg)
        return messages