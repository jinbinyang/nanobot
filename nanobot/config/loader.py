"""
配置加载工具模块 (config/loader.py)
=================================
本模块负责 nanobot 配置文件的加载、保存和格式转换：
- 配置文件默认路径: ~/.nanobot/config.json
- 配置文件使用 camelCase（驼峰命名），Python 内部使用 snake_case（下划线命名）
- 加载时自动将 camelCase → snake_case，保存时自动将 snake_case → camelCase
- 支持旧版配置格式的自动迁移

对于 Java 开发者：
- 类似于 Spring Boot 的 application.yml 加载机制
- camelCase ↔ snake_case 转换类似于 Jackson 的 @JsonNaming 注解功能
"""

import json
from pathlib import Path
from typing import Any

from nanobot.config.schema import Config


def get_config_path() -> Path:
    """获取默认配置文件路径: ~/.nanobot/config.json"""
    return Path.home() / ".nanobot" / "config.json"


def get_data_dir() -> Path:
    """获取 nanobot 数据目录（存放工作区、日志等运行时数据）"""
    from nanobot.utils.helpers import get_data_path
    return get_data_path()


def load_config(config_path: Path | None = None) -> Config:
    """
    从 JSON 文件加载配置，若文件不存在则返回默认配置。

    加载流程：
    1. 确定配置文件路径（传入的路径 或 默认路径 ~/.nanobot/config.json）
    2. 读取 JSON 文件内容
    3. 执行旧版配置格式迁移（_migrate_config）
    4. 将 camelCase 键名转换为 snake_case（convert_keys）
    5. 使用 Pydantic 的 model_validate 进行类型验证和反序列化

    参数:
        config_path: 可选的配置文件路径。为 None 时使用默认路径。

    返回:
        Config 配置对象实例
    """
    path = config_path or get_config_path()
    
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            data = _migrate_config(data)  # 处理旧版配置格式兼容
            return Config.model_validate(convert_keys(data))  # 键名转换 + Pydantic 验证
        except (json.JSONDecodeError, ValueError) as e:
            # 配置文件损坏时降级使用默认配置，而非直接报错退出
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")
    
    return Config()  # 文件不存在或解析失败时返回默认配置


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    将配置对象保存为 JSON 文件。

    保存流程：
    1. 将 Pydantic 模型序列化为 dict（snake_case 键名）
    2. 转换为 camelCase 键名（与 JSON 文件格式保持一致）
    3. 写入 JSON 文件（带缩进格式化）

    参数:
        config: 要保存的配置对象
        config_path: 可选的保存路径。为 None 时使用默认路径。
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)  # 自动创建父目录
    
    # 序列化为 dict 并转换为 camelCase 格式
    data = config.model_dump()
    data = convert_to_camel(data)
    
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _migrate_config(data: dict) -> dict:
    """
    旧版配置格式迁移。

    迁移规则：将 tools.exec.restrictToWorkspace 移至 tools.restrictToWorkspace
    这是因为早期版本中该配置项嵌套在 exec 下，后来提升为 tools 级别的全局配置。

    参数:
        data: 原始配置字典

    返回:
        迁移后的配置字典
    """
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    # 如果旧位置有值且新位置没有，则自动迁移
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data


def convert_keys(data: Any) -> Any:
    """
    递归地将字典中所有 camelCase 键名转换为 snake_case。
    用于从 JSON 配置文件加载时的键名适配。

    示例: {"maxTokens": 8192} → {"max_tokens": 8192}

    参数:
        data: 任意数据（dict/list/基本类型）

    返回:
        键名转换后的数据
    """
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """
    递归地将字典中所有 snake_case 键名转换为 camelCase。
    用于保存配置到 JSON 文件时的键名适配。

    示例: {"max_tokens": 8192} → {"maxTokens": 8192}

    参数:
        data: 任意数据（dict/list/基本类型）

    返回:
        键名转换后的数据
    """
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """
    将 camelCase 字符串转换为 snake_case。
    例: "maxTokens" → "max_tokens", "apiBase" → "api_base"

    实现原理：遇到大写字母时在其前面插入下划线，然后全部转小写。
    """
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")  # 在大写字母前插入下划线
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """
    将 snake_case 字符串转换为 camelCase。
    例: "max_tokens" → "maxTokens", "api_base" → "apiBase"

    实现原理：按下划线分割，第一段保持小写，后续每段首字母大写后拼接。
    """
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])