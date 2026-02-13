"""
语音转文字（Speech-to-Text）提供者模块。

本模块负责将用户发送的语音消息转换为文字，使得 Agent 能够"听懂"语音输入。
当前实现基于 Groq 平台的 Whisper 大模型（whisper-large-v3），特点是速度极快且有免费额度。

在语音交互多智能体系统的二次开发中，本模块是"语音输入"的关键入口：
  用户语音 → 渠道接收音频文件 → GroqTranscriptionProvider.transcribe() → 文字 → Agent 处理

如需扩展：
  - 可添加其他 STT 服务（如 OpenAI Whisper、阿里云 ASR、讯飞等）
  - 可添加 TTS（文字转语音）功能实现完整的语音交互闭环

技术说明：
  - 使用 httpx（Python 的异步 HTTP 客户端，类似 Java 的 OkHttp）发送请求
  - 通过 Groq 的 OpenAI 兼容接口上传音频文件并获取转录结果
"""

import os
from pathlib import Path
from typing import Any

import httpx  # 异步 HTTP 客户端库（类似 Java 的 OkHttp/WebClient）
from loguru import logger  # 结构化日志库（类似 Java 的 SLF4J/Logback）


class GroqTranscriptionProvider:
    """
    基于 Groq Whisper API 的语音转文字服务提供者。

    Groq 是一家专注于推理加速的公司，其 Whisper API 速度极快（通常几秒内完成转录），
    并提供慷慨的免费额度，非常适合开发和测试。

    使用方式：
        provider = GroqTranscriptionProvider(api_key="gsk_xxx")
        text = await provider.transcribe("/path/to/audio.ogg")

    属性：
        api_key: Groq 平台的 API 密钥
        api_url: Groq Whisper API 的端点地址
    """

    def __init__(self, api_key: str | None = None):
        # 优先使用传入的 api_key，否则从环境变量读取（与 Java 的 @Value 注入类似）
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        # Groq 的 Whisper API 遵循 OpenAI 兼容格式
        self.api_url = "https://api.groq.com/openai/v1/audio/transcriptions"

    async def transcribe(self, file_path: str | Path) -> str:
        """
        将音频文件转录为文字。

        参数：
            file_path: 音频文件的本地路径（支持 ogg、mp3、wav、m4a 等格式）

        返回：
            转录后的文本字符串；如果失败则返回空字符串

        注意：
            - 此方法是异步的（async），需要在异步上下文中调用
            - 超时设置为 60 秒，对于较长的音频可能需要调整
        """
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error(f"Audio file not found: {file_path}")
            return ""

        try:
            # 使用 httpx 的异步客户端发送 multipart/form-data 请求
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    # 构造 multipart 表单数据：文件 + 模型名称
                    files = {
                        "file": (path.name, f),          # 上传的音频文件
                        "model": (None, "whisper-large-v3"),  # 使用 Whisper V3 大模型
                    }
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",  # Bearer Token 认证
                    }

                    response = await client.post(
                        self.api_url,
                        headers=headers,
                        files=files,
                        timeout=60.0  # 60 秒超时，长音频可能需要加大
                    )

                    response.raise_for_status()  # 如果 HTTP 状态码非 2xx，抛出异常
                    data = response.json()
                    return data.get("text", "")  # 从 JSON 响应中提取转录文本

        except Exception as e:
            logger.error(f"Groq transcription error: {e}")
            return ""  # 出错时返回空字符串，不中断主流程