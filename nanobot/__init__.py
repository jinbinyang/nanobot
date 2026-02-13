"""
nanobot - 轻量级 AI Agent 框架

模块概述：
    本文件是 nanobot 包的入口文件（__init__.py），定义了包的元信息。
    nanobot 是香港大学开源的 OpenClaw（clawdbot）的轻量版实现，
    目标是提供一个极简但功能完整的 AI Agent 框架。

    整个框架的核心功能包括：
    - 多渠道消息接入（Telegram、Discord、飞书、钉钉等）
    - 基于 LLM 的智能对话（通过 LiteLLM 统一适配多家模型）
    - 工具调用（文件操作、Shell 命令、Web 搜索等）
    - 子代理（SubAgent）与技能（Skills）系统
    - 定时任务、心跳检测等辅助功能
"""

# 版本号，遵循语义化版本规范（主版本.次版本.修订号）
__version__ = "0.1.0"

# 项目 logo 表情符号，用于 CLI 输出等场景的品牌标识
__logo__ = "🐈"
