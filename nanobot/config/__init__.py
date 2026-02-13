"""
配置模块 (config)
================
本模块是 nanobot 的配置系统入口，负责：
1. 定义配置数据模型（schema.py）—— 使用 Pydantic 定义所有配置项的结构和默认值
2. 加载/保存配置文件（loader.py）—— 从 JSON 文件读取配置，支持 camelCase ↔ snake_case 自动转换

对于 Java 开发者：
- Pydantic 的 BaseModel 类似于 Java 中的 POJO/DTO，但自带数据验证功能
- Config 类似于 Spring Boot 的 @ConfigurationProperties，将配置文件映射为类型安全的对象
"""

from nanobot.config.loader import load_config, get_config_path  # 导出配置加载函数
from nanobot.config.schema import Config  # 导出根配置类

__all__ = ["Config", "load_config", "get_config_path"]
