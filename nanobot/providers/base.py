"""
LLM 提供者基类定义模块。

本模块定义了与大语言模型交互的核心抽象接口，类似于 Java 中的 Interface + DTO 模式：
- ToolCallRequest : LLM 返回的工具调用请求（当 LLM 决定调用某个工具时的数据结构）
- LLMResponse     : LLM 的统一响应格式（包含文本内容、工具调用、token 用量等）
- LLMProvider     : 抽象基类（Abstract Base Class），定义了所有 LLM 提供者必须实现的接口

架构角色：
  用户消息 → Agent 循环 → LLMProvider.chat() → LLM API → LLMResponse → Agent 处理

类比 Java：
  - LLMProvider 相当于一个 interface，定义了 chat() 和 getDefaultModel() 方法
  - LLMResponse 相当于一个不可变的 DTO（Data Transfer Object）
  - ToolCallRequest 相当于一个 POJO，承载工具调用的参数信息
"""

from abc import ABC, abstractmethod  # ABC = Abstract Base Class，Python 的接口机制
from dataclasses import dataclass, field  # dataclass 类似 Java 的 @Data（Lombok）
from typing import Any


@dataclass
class ToolCallRequest:
    """
    LLM 返回的工具调用请求。

    当 LLM 判断需要调用某个工具（如读文件、执行命令等）时，会返回这个结构。
    类似于 Java 中的一个简单 POJO。

    属性：
        id: 工具调用的唯一标识符（由 LLM API 生成，用于将工具结果与请求关联）
        name: 要调用的工具名称（如 "read_file"、"shell" 等）
        arguments: 工具调用的参数字典（如 {"path": "/tmp/test.txt"}）
    """
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """
    LLM 的统一响应数据结构。

    无论底层使用哪家 LLM 服务商（OpenAI、Anthropic、DeepSeek 等），
    都会被统一解析成这个格式，屏蔽了各家 API 的差异。

    属性：
        content: LLM 返回的文本内容（可能为 None，当 LLM 只返回工具调用时）
        tool_calls: LLM 请求调用的工具列表（可以同时调用多个工具）
        finish_reason: 结束原因（"stop"=正常结束, "tool_calls"=需要调用工具, "error"=出错）
        usage: token 用量统计（prompt_tokens, completion_tokens, total_tokens）
        reasoning_content: 推理内容（部分模型如 Kimi、DeepSeek-R1 会返回思考过程）
    """
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi、DeepSeek-R1 等模型会返回推理过程

    @property
    def has_tool_calls(self) -> bool:
        """检查响应中是否包含工具调用请求。"""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    LLM 提供者抽象基类（类似 Java 的 interface）。

    所有 LLM 服务的实现类都必须继承此类并实现两个抽象方法：
    - chat()             : 发送对话请求并获取响应
    - get_default_model(): 返回该提供者的默认模型名称

    当前项目中唯一的实现类是 LiteLLMProvider（在 litellm_provider.py 中）。
    如果你需要接入一个 LiteLLM 不支持的 LLM 服务，可以新建一个实现类继承此基类。

    属性：
        api_key: API 密钥
        api_base: API 基础 URL（用于自定义端点或代理）
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        发送对话补全请求（核心方法）。

        参数：
            messages: 消息列表，每条消息是 {"role": "user/assistant/system", "content": "..."} 格式
            tools: 可选的工具定义列表（OpenAI 函数调用格式）
            model: 模型标识符（如 'anthropic/claude-sonnet-4-5'）
            max_tokens: 响应的最大 token 数
            temperature: 采样温度（0.0=确定性输出, 1.0=更多随机性）

        返回：
            LLMResponse，包含文本内容和/或工具调用请求
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """获取该提供者的默认模型名称。"""
        pass