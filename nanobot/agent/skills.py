"""
技能加载器模块 - 管理 Agent 可用的技能（Skills）。

本模块实现了 nanobot 的"技能系统"，技能本质上是 Markdown 格式的指令文件（SKILL.md），
用于教会 Agent 如何使用特定工具或执行特定任务。这是一种"提示词工程"的模块化方案。

【技能的本质】
每个技能就是一个目录，里面包含一个 SKILL.md 文件，该文件用自然语言描述了：
- 技能的用途和适用场景
- 使用步骤和注意事项
- 可能附带的脚本文件路径

Agent 在构建上下文时，会将相关技能的内容注入到 system prompt 中，
从而"教会"LLM 如何完成特定任务。

【技能来源（双层优先级）】
1. 工作区技能（workspace/skills/）：用户自定义，优先级最高
2. 内置技能（nanobot/skills/）：项目自带，作为兜底

【Java 开发者类比】
可以把技能系统理解为一个"插件加载器"：
- SKILL.md 文件类似于 Spring 的 XML 配置或注解配置
- 技能目录类似于 SPI（Service Provider Interface）的实现包
- 前端元数据（frontmatter）类似于 Maven 的 pom.xml 描述信息

【二开提示】
在多 Agent 系统中，可以为不同领域的 Agent 分配不同的技能集，
例如：天气 Agent 只加载 weather 技能，GitHub Agent 只加载 github 技能，
而管家 Agent 加载所有技能的摘要用于路由决策。
"""

import json
import os
import re
import shutil
from pathlib import Path

# 内置技能目录的默认路径（相对于本文件的上级目录下的 skills/）
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    技能加载器 - 负责发现、加载和管理 Agent 的技能。

    核心功能：
    1. 扫描工作区和内置目录，发现所有可用技能
    2. 按名称加载技能内容，供 Agent 上下文使用
    3. 检查技能的前置依赖（命令行工具、环境变量）
    4. 生成技能摘要列表，支持渐进式加载

    属性:
        workspace: 工作区根目录
        workspace_skills: 工作区技能目录（workspace/skills/）
        builtin_skills: 内置技能目录（nanobot/skills/）
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        """
        初始化技能加载器。

        参数:
            workspace: 工作区根目录路径
            builtin_skills_dir: 内置技能目录路径，None 时使用默认路径
        """
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        列出所有可用的技能。

        扫描顺序：先扫描工作区技能（优先级高），再扫描内置技能。
        同名技能以工作区版本为准（工作区可覆盖内置技能）。

        参数:
            filter_unavailable: 是否过滤掉依赖不满足的技能，默认 True

        返回:
            技能信息字典列表，每个字典包含 'name'（名称）、'path'（文件路径）、'source'（来源）
        """
        skills = []

        # 第一优先级：工作区技能
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # 第二优先级：内置技能（跳过与工作区同名的技能，避免重复）
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # 根据参数决定是否过滤掉依赖不满足的技能
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        按名称加载单个技能的完整内容。

        查找顺序：工作区 → 内置目录

        参数:
            name: 技能名称（即目录名）

        返回:
            技能的 Markdown 文本内容，未找到时返回 None
        """
        # 优先从工作区加载
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # 回退到内置技能
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        批量加载指定技能，格式化后用于注入 Agent 上下文。

        该方法会去除每个技能的 YAML frontmatter（元数据头），
        只保留正文内容，并用分隔线连接多个技能。

        参数:
            skill_names: 要加载的技能名称列表

        返回:
            格式化后的技能内容字符串，用 '---' 分隔各技能
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                # 去除 frontmatter，只保留正文
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        构建所有技能的摘要列表（XML格式）。

        这是"渐进式加载"策略的核心：不一次性加载所有技能的完整内容，
        而是先给 Agent 一个摘要目录，Agent 需要时再通过 read_file 工具
        读取特定技能的完整内容。这样可以显著减少 token 消耗。

        返回:
            XML 格式的技能摘要字符串，包含名称、描述、路径、可用状态
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            """转义 XML 特殊字符，防止注入"""
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # 对不可用的技能，显示缺失的依赖信息，方便排查
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append(f"  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """
        获取技能缺失的依赖描述。

        参数:
            skill_meta: 技能的 nanobot 元数据字典

        返回:
            缺失依赖的描述字符串，如 "CLI: tmux, ENV: GITHUB_TOKEN"
        """
        missing = []
        requires = skill_meta.get("requires", {})
        # 检查命令行工具是否已安装（通过 which 命令）
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        # 检查环境变量是否已设置
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """
        获取技能的描述信息（从 frontmatter 中提取）。

        参数:
            name: 技能名称

        返回:
            技能描述字符串，没有描述时回退为技能名称
        """
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # 回退：用技能名称本身作为描述

    def _strip_frontmatter(self, content: str) -> str:
        """
        去除 Markdown 文件的 YAML frontmatter 头部。

        YAML frontmatter 是以 '---' 包裹的元数据块，例如：
        ---
        description: 天气查询技能
        always: true
        ---

        参数:
            content: Markdown 文件的完整内容

        返回:
            去除 frontmatter 后的正文内容
        """
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: str) -> dict:
        """
        解析技能 frontmatter 中的 nanobot 元数据 JSON。

        技能的 frontmatter 中可以包含一个 'metadata' 字段，
        其值为 JSON 字符串，用于描述 nanobot 特有的配置（如依赖要求）。

        参数:
            raw: metadata 字段的 JSON 字符串

        返回:
            解析后的 nanobot 配置字典，解析失败时返回空字典
        """
        try:
            data = json.loads(raw)
            # 从 JSON 中提取 "nanobot" 键对应的配置
            return data.get("nanobot", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """
        检查技能的前置依赖是否满足。

        支持两种依赖类型：
        - bins: 命令行工具（通过 shutil.which 检查是否在 PATH 中）
        - env: 环境变量（通过 os.environ.get 检查是否已设置）

        参数:
            skill_meta: 技能的 nanobot 元数据字典

        返回:
            True 表示所有依赖都满足，False 表示存在缺失依赖
        """
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """
        获取技能的 nanobot 专有元数据。

        该方法先获取 frontmatter，再从中提取并解析 'metadata' 字段。

        参数:
            name: 技能名称

        返回:
            nanobot 元数据字典
        """
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """
        获取标记为"始终加载"的技能列表。

        某些技能通过 frontmatter 中的 always: true 标记，
        表示无论用户是否显式请求，都应该加载到 Agent 上下文中。

        返回:
            始终加载且依赖满足的技能名称列表
        """
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            # 检查 nanobot 元数据或顶层 frontmatter 中的 always 标记
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        从技能的 YAML frontmatter 中提取元数据。

        使用简单的逐行解析替代完整的 YAML 解析器（避免引入 PyYAML 依赖），
        支持 'key: value' 格式的简单键值对。

        参数:
            name: 技能名称

        返回:
            元数据字典，技能不存在或无 frontmatter 时返回 None
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # 简易 YAML 解析：逐行按冒号分割键值
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None