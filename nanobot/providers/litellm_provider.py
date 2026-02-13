"""
LiteLLM 提供者实现模块 —— 多 LLM 服务商的统一调用层。

本模块是 LLMProvider 抽象基类的唯一实现，通过 LiteLLM 开源库实现了
"一套代码对接所有主流 LLM 服务商"的能力。

LiteLLM 是什么？
  LiteLLM 是一个 Python 库，它将 100+ 家 LLM 服务商（OpenAI、Anthropic、Google、
  DeepSeek、通义千问等）的 API 统一为 OpenAI 兼容格式。
  类比 Java 世界：LiteLLM 类似于 JDBC —— 一套接口，多种数据库驱动。

核心设计：
  1. 模型名称解析：根据 registry.py 中的元数据，自动为模型名添加正确的前缀
     例如 "qwen-max" → "dashscope/qwen-max"（LiteLLM 需要前缀来路由到正确的服务商）
  2. 网关检测：自动识别 OpenRouter、AiHubMix 等 API 网关，应用特殊的路由规则
  3. 环境变量配置：根据检测到的服务商，自动设置 LiteLLM 所需的环境变量
  4. 错误容错：LLM 调用失败时返回错误信息而非抛出异常，保证主流程不中断

数据流：
  Agent.loop() → LiteLLMProvider.chat() → _resolve_model() → LiteLLM.acompletion() → LLM API
                                                                      ↓
  Agent.loop() ← _parse_response() ← LLMResponse ← API Response ←──┘
"""

import json
import os
from typing import Any

import litellm  # LiteLLM 库：多 LLM 服务商的统一调用层（类似 Java 的 JDBC）
from litellm import acompletion  # 异步对话补全函数（a = async）

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.registry import find_by_model, find_gateway


class LiteLLMProvider(LLMProvider):
    """
    基于 LiteLLM 的 LLM 提供者实现类。

    支持 OpenRouter、Anthropic、OpenAI、Gemini、MiniMax、DeepSeek、通义千问
    等众多服务商，通过统一接口完成对话补全。

    服务商特定的逻辑（前缀、环境变量、参数覆盖等）由 registry.py 驱动，
    本类中无需编写 if-elif 分支链。

    构造参数：
        api_key: API 密钥
        api_base: 自定义 API 基础 URL（用于代理/网关/本地部署）
        default_model: 默认模型名称（如 "anthropic/claude-opus-4-5"）
        extra_headers: 额外的 HTTP 请求头（如 AiHubMix 需要的 APP-Code）
        provider_name: 配置文件中的提供者名称（如 "dashscope"），用于网关/本地部署检测
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        # 检测是否通过网关（如 OpenRouter）或本地部署（如 vLLM）访问
        # provider_name（来自配置文件的 key）是首要信号；api_key/api_base 是备选检测方式
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # 根据检测到的服务商，设置 LiteLLM 所需的环境变量
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        if api_base:
            litellm.api_base = api_base

        # 禁用 LiteLLM 的调试日志输出（默认很啰嗦）
        litellm.suppress_debug_info = True
        # 自动丢弃服务商不支持的参数（避免因多余参数导致请求失败）
        litellm.drop_params = True

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """
        根据检测到的服务商设置环境变量。

        LiteLLM 内部通过环境变量查找各服务商的 API Key，
        例如 Anthropic 需要 ANTHROPIC_API_KEY，DashScope 需要 DASHSCOPE_API_KEY。
        本方法根据 registry 中的 ProviderSpec 元数据自动设置这些环境变量。

        参数：
            api_key: 用户配置的 API 密钥
            api_base: 用户配置的 API 基础 URL
            model: 模型名称（用于匹配服务商）
        """
        # 优先使用网关的 spec，否则根据模型名查找对应的服务商 spec
        spec = self._gateway or find_by_model(model)
        if not spec:
            return

        # 网关/本地部署：强制覆盖环境变量（因为网关的 key 不同于原始服务商的 key）
        # 标准服务商：使用 setdefault，不覆盖已有的环境变量
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # 解析 env_extras 中的占位符并设置额外的环境变量
        # 占位符：{api_key} → 用户的 API Key, {api_base} → API 基础 URL
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _resolve_model(self, model: str) -> str:
        """
        解析模型名称，添加 LiteLLM 所需的服务商前缀。

        LiteLLM 通过模型名前缀来路由到正确的服务商：
        - "deepseek/deepseek-chat" → 路由到 DeepSeek
        - "dashscope/qwen-max" → 路由到通义千问
        - "openrouter/claude-3" → 路由到 OpenRouter 网关

        参数：
            model: 原始模型名称（可能带或不带前缀）

        返回：
            处理后的模型名称（带有正确的 LiteLLM 前缀）
        """
        if self._gateway:
            # 网关模式：应用网关前缀，跳过服务商特定的前缀
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                # 某些网关（如 AiHubMix）不理解 "anthropic/claude-3"，
                # 需要先剥离为 "claude-3"，再加上网关前缀 "openai/claude-3"
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model

        # 标准模式：为已知服务商的模型自动添加前缀
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            # 检查是否已有前缀（避免重复添加，如 "deepseek/deepseek-chat" 不变）
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """
        应用特定模型的参数覆盖规则。

        某些模型有特殊的参数要求，例如：
        - Kimi K2.5 要求 temperature >= 1.0（否则 API 会报错）

        这些规则在 registry.py 的 ProviderSpec.model_overrides 中定义。

        参数：
            model: 模型名称
            kwargs: 即将传给 LiteLLM 的参数字典（会被就地修改）
        """
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)  # 就地更新参数
                    return

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

        这是整个 nanobot 中最关键的方法之一 —— Agent 的每一轮"思考"都会调用此方法。
        它将消息和工具定义发送给 LLM，获取 LLM 的回复（可能是文本，也可能是工具调用请求）。

        参数：
            messages: 对话消息列表，每条为 {"role": "user/assistant/system", "content": "..."}
            tools: 可选的工具定义列表（OpenAI 函数调用格式，定义了 Agent 可使用的工具）
            model: 模型标识符（如 'anthropic/claude-sonnet-4-5'），为空则使用默认模型
            max_tokens: 响应的最大 token 数
            temperature: 采样温度（0.0=确定性, 1.0=更随机）

        返回：
            LLMResponse：统一的响应格式，包含文本内容和/或工具调用请求
        """
        # 步骤1：解析模型名称，添加 LiteLLM 所需的前缀
        model = self._resolve_model(model or self.default_model)

        # 步骤2：构建 LiteLLM 调用参数
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # 步骤3：应用特定模型的参数覆盖（如 Kimi K2.5 的 temperature 限制）
        self._apply_model_overrides(model, kwargs)

        # 步骤4：传入认证信息（直接传 api_key 比仅依赖环境变量更可靠）
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # 传入自定义端点（用于代理/网关/本地部署场景）
        if self.api_base:
            kwargs["api_base"] = self.api_base

        # 传入额外请求头（如 AiHubMix 需要的 APP-Code 认证头）
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        # 步骤5：如果提供了工具定义，告知 LLM 可以调用工具
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"  # 让 LLM 自主决定是否调用工具

        try:
            # 步骤6：调用 LiteLLM 的异步对话补全 API
            response = await acompletion(**kwargs)
            # 步骤7：将 LiteLLM 响应解析为统一的 LLMResponse 格式
            return self._parse_response(response)
        except Exception as e:
            # 出错时返回错误信息而非抛出异常，保证 Agent 循环不中断
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    def _parse_response(self, response: Any) -> LLMResponse:
        """
        将 LiteLLM 的原始响应解析为统一的 LLMResponse 格式。

        LiteLLM 的响应格式遵循 OpenAI 规范：
        response.choices[0].message 中包含：
          - content: 文本回复
          - tool_calls: 工具调用请求列表
          - reasoning_content: 推理过程（部分模型支持）

        参数：
            response: LiteLLM 返回的原始响应对象

        返回：
            标准化的 LLMResponse 对象
        """
        choice = response.choices[0]  # OpenAI 格式：choices 数组，通常只有一个元素
        message = choice.message

        # 解析工具调用请求
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # 工具参数可能是 JSON 字符串，需要解析为字典
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}  # JSON 解析失败时保留原始字符串

                tool_calls.append(ToolCallRequest(
                    id=tc.id,                    # 工具调用 ID（用于后续关联结果）
                    name=tc.function.name,       # 工具名称（如 "read_file"）
                    arguments=args,              # 工具参数字典
                ))

        # 解析 token 用量统计
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,       # 输入 token 数
                "completion_tokens": response.usage.completion_tokens,  # 输出 token 数
                "total_tokens": response.usage.total_tokens,         # 总 token 数
            }

        # 提取推理内容（Kimi、DeepSeek-R1 等模型的"思考过程"）
        reasoning_content = getattr(message, "reasoning_content", None)

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
        )

    def get_default_model(self) -> str:
        """获取默认模型名称。"""
        return self.default_model