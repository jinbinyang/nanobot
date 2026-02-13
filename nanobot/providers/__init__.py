"""
LLM 提供者抽象层模块（providers 包）。

本模块是 nanobot 与各种大语言模型（LLM）服务之间的桥梁层。
核心设计思想：通过 LiteLLM 库实现"一套接口，多家 LLM"的统一调用。

模块组成：
- base.py       : 定义 LLMProvider 抽象基类和 LLMResponse 数据结构（类似 Java 的接口 + DTO）
- litellm_provider.py : LLMProvider 的唯一实现类，基于 LiteLLM 库对接所有 LLM 服务商
- registry.py   : 提供者注册表，集中管理所有 LLM 服务商的元数据（API Key、模型前缀等）
- transcription.py : 语音转文字服务，基于 Groq 的 Whisper API

对于二次开发（语音交互多智能体系统）来说，本模块是核心依赖之一：
- Agent 的"思考能力"来自 LLMProvider.chat()
- 语音输入的"听力"来自 GroqTranscriptionProvider.transcribe()
- 如需接入新的 LLM 服务商，只需在 registry.py 中添加一条 ProviderSpec 即可
"""

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.litellm_provider import LiteLLMProvider

__all__ = ["LLMProvider", "LLMResponse", "LiteLLMProvider"]
