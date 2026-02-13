"""
LLM 提供者注册表 —— 所有 LLM 服务商元数据的唯一真相来源（Single Source of Truth）。

本模块是 nanobot 多服务商支持的核心配置中心，采用"数据驱动"的设计思想：
所有服务商的差异（API Key 环境变量名、模型前缀、特殊参数等）都集中在 PROVIDERS 元组中定义，
而非散落在代码各处的 if-elif 分支中。

这种设计的好处（类比 Java）：
  - 传统做法：在代码中用 if (provider == "openai") ... else if (provider == "anthropic") ...
  - 本项目做法：类似于用一个"配置表"（类似 Spring 的 application.yml 中的配置映射），
    每个服务商的差异都声明在 ProviderSpec 数据类中，代码逻辑完全通用。

添加新的 LLM 服务商只需两步：
  1. 在下方 PROVIDERS 元组中新增一条 ProviderSpec
  2. 在 config/schema.py 的 ProvidersConfig 中新增一个字段
  完毕。环境变量设置、模型前缀、配置匹配、状态显示等功能全部自动派生。

PROVIDERS 中的顺序很重要 —— 它决定了匹配优先级和回退顺序。网关类型排在最前面。
每条记录都显式写出所有字段，方便直接复制粘贴作为模板使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)  # frozen=True 使实例不可变（类似 Java 的 final + record）
class ProviderSpec:
    """
    单个 LLM 服务商的元数据规格定义。

    这是一个不可变的数据类，描述了一个 LLM 服务商的所有特征。
    类似于 Java 中的 record 类型或带 @Value 的 Lombok 类。

    属性分组说明：

    【身份标识】
        name: 配置字段名（如 "dashscope"），对应 config.yaml 中的 key
        keywords: 模型名关键词元组，用于根据模型名匹配服务商（全小写）
        env_key: LiteLLM 需要的环境变量名（如 "DASHSCOPE_API_KEY"）
        display_name: 在 `nanobot status` 命令中显示的名称

    【模型前缀】
        litellm_prefix: LiteLLM 路由前缀（如 "dashscope" → 模型变为 "dashscope/{model}"）
        skip_prefixes: 如果模型名已有这些前缀则跳过添加（避免重复前缀）

    【额外环境变量】
        env_extras: 需要设置的额外环境变量，支持 {api_key} 和 {api_base} 占位符

    【网关/本地部署检测】
        is_gateway: 是否是 API 网关（如 OpenRouter，可路由任意模型）
        is_local: 是否是本地部署（如 vLLM、Ollama）
        detect_by_key_prefix: 通过 API Key 前缀自动检测（如 "sk-or-" → OpenRouter）
        detect_by_base_keyword: 通过 API Base URL 关键词检测（如 "aihubmix"）
        default_api_base: 默认的 API 基础 URL

    【网关行为】
        strip_model_prefix: 是否在重新添加网关前缀前剥离原有的服务商前缀

    【模型级参数覆盖】
        model_overrides: 特定模型的参数覆盖规则（如 Kimi K2.5 需要 temperature >= 1.0）
    """

    # --- 身份标识 ---
    name: str                       # 配置字段名，如 "dashscope"
    keywords: tuple[str, ...]       # 模型名关键词（小写），用于匹配
    env_key: str                    # LiteLLM 环境变量名，如 "DASHSCOPE_API_KEY"
    display_name: str = ""          # 显示名称，用于 `nanobot status`

    # --- 模型前缀 ---
    litellm_prefix: str = ""                 # LiteLLM 路由前缀
    skip_prefixes: tuple[str, ...] = ()      # 已有这些前缀时跳过添加

    # --- 额外环境变量 ---
    env_extras: tuple[tuple[str, str], ...] = ()  # 如 (("ZHIPUAI_API_KEY", "{api_key}"),)

    # --- 网关/本地部署检测 ---
    is_gateway: bool = False                 # 是否是 API 网关（可路由任意模型）
    is_local: bool = False                   # 是否是本地部署
    detect_by_key_prefix: str = ""           # API Key 前缀检测（如 "sk-or-"）
    detect_by_base_keyword: str = ""         # API Base URL 关键词检测
    default_api_base: str = ""               # 默认 API 基础 URL

    # --- 网关行为 ---
    strip_model_prefix: bool = False         # 是否剥离模型名中的服务商前缀

    # --- 模型级参数覆盖 ---
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()  # 如 (("kimi-k2.5", {"temperature": 1.0}),)

    @property
    def label(self) -> str:
        """获取显示标签，优先使用 display_name，否则将 name 首字母大写。"""
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS —— 服务商注册表。顺序 = 优先级。可复制任意条目作为模板。
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (

    # ===== 网关类型（通过 api_key / api_base 检测，而非模型名）=====
    # 网关可以路由任意模型，因此在回退匹配中优先级最高。

    # OpenRouter：全球 API 网关，Key 以 "sk-or-" 开头
    ProviderSpec(
        name="openrouter",
        keywords=("openrouter",),
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        litellm_prefix="openrouter",        # claude-3 → openrouter/claude-3
        skip_prefixes=(),
        env_extras=(),
        is_gateway=True,                    # 标记为网关
        is_local=False,
        detect_by_key_prefix="sk-or-",      # 通过 Key 前缀自动检测
        detect_by_base_keyword="openrouter", # 通过 URL 关键词自动检测
        default_api_base="https://openrouter.ai/api/v1",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # AiHubMix：全球 API 网关，OpenAI 兼容接口。
    # strip_model_prefix=True：AiHubMix 不理解 "anthropic/claude-3"，
    # 需要先剥离为 "claude-3"，再添加 "openai/" 前缀变为 "openai/claude-3"
    ProviderSpec(
        name="aihubmix",
        keywords=("aihubmix",),
        env_key="OPENAI_API_KEY",           # 使用 OpenAI 兼容接口
        display_name="AiHubMix",
        litellm_prefix="openai",            # → openai/{model}
        skip_prefixes=(),
        env_extras=(),
        is_gateway=True,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="aihubmix",  # 通过 URL 关键词检测
        default_api_base="https://aihubmix.com/v1",
        strip_model_prefix=True,            # anthropic/claude-3 → claude-3 → openai/claude-3
        model_overrides=(),
    ),

    # ===== 标准服务商（通过模型名关键词匹配）=====

    # Anthropic：LiteLLM 原生识别 "claude-*"，无需前缀
    ProviderSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        litellm_prefix="",                  # 无需前缀，LiteLLM 直接识别
        skip_prefixes=(),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # OpenAI：LiteLLM 原生识别 "gpt-*"，无需前缀
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        litellm_prefix="",                  # 无需前缀
        skip_prefixes=(),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # DeepSeek：需要 "deepseek/" 前缀让 LiteLLM 正确路由
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        litellm_prefix="deepseek",          # deepseek-chat → deepseek/deepseek-chat
        skip_prefixes=("deepseek/",),       # 避免重复前缀
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # Gemini（Google）：需要 "gemini/" 前缀
    ProviderSpec(
        name="gemini",
        keywords=("gemini",),
        env_key="GEMINI_API_KEY",
        display_name="Gemini",
        litellm_prefix="gemini",            # gemini-pro → gemini/gemini-pro
        skip_prefixes=("gemini/",),         # 避免重复前缀
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # 智谱 AI：LiteLLM 使用 "zai/" 前缀路由
    # 同时将 Key 镜像到 ZHIPUAI_API_KEY（LiteLLM 某些路径会检查该变量）
    ProviderSpec(
        name="zhipu",
        keywords=("zhipu", "glm", "zai"),
        env_key="ZAI_API_KEY",
        display_name="Zhipu AI",
        litellm_prefix="zai",              # glm-4 → zai/glm-4
        skip_prefixes=("zhipu/", "zai/", "openrouter/", "hosted_vllm/"),
        env_extras=(
            ("ZHIPUAI_API_KEY", "{api_key}"),  # 镜像 Key 到另一个环境变量
        ),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # 通义千问（DashScope）：阿里云的 LLM 服务，需要 "dashscope/" 前缀
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        litellm_prefix="dashscope",         # qwen-max → dashscope/qwen-max
        skip_prefixes=("dashscope/", "openrouter/"),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # Moonshot（月之暗面）：Kimi 系列模型，需要 "moonshot/" 前缀
    # LiteLLM 需要 MOONSHOT_API_BASE 环境变量来定位端点
    # Kimi K2.5 API 强制要求 temperature >= 1.0
    ProviderSpec(
        name="moonshot",
        keywords=("moonshot", "kimi"),
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot",
        litellm_prefix="moonshot",          # kimi-k2.5 → moonshot/kimi-k2.5
        skip_prefixes=("moonshot/", "openrouter/"),
        env_extras=(
            ("MOONSHOT_API_BASE", "{api_base}"),  # LiteLLM 需要此环境变量
        ),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="https://api.moonshot.ai/v1",   # 国际站；国内使用 api.moonshot.cn
        strip_model_prefix=False,
        model_overrides=(
            ("kimi-k2.5", {"temperature": 1.0}),  # Kimi K2.5 强制 temperature >= 1.0
        ),
    ),

    # MiniMax：需要 "minimax/" 前缀，使用 OpenAI 兼容 API
    ProviderSpec(
        name="minimax",
        keywords=("minimax",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax",
        litellm_prefix="minimax",            # MiniMax-M2.1 → minimax/MiniMax-M2.1
        skip_prefixes=("minimax/", "openrouter/"),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="https://api.minimax.io/v1",
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # ===== 本地部署（通过配置 key 匹配，而非 api_base）=====

    # vLLM / 任意 OpenAI 兼容的本地服务
    # 当配置文件中的 key 为 "vllm" 时（provider_name="vllm"）触发检测
    ProviderSpec(
        name="vllm",
        keywords=("vllm",),
        env_key="HOSTED_VLLM_API_KEY",
        display_name="vLLM/Local",
        litellm_prefix="hosted_vllm",      # Llama-3-8B → hosted_vllm/Llama-3-8B
        skip_prefixes=(),
        env_extras=(),
        is_gateway=False,
        is_local=True,                      # 标记为本地部署
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",                # 用户必须在配置中提供 api_base
        strip_model_prefix=False,
        model_overrides=(),
    ),

    # ===== 辅助服务（非主要 LLM 提供者）=====

    # Groq：主要用于 Whisper 语音转录，也可用于 LLM 推理
    # 需要 "groq/" 前缀。放在最后 —— 很少作为主力 LLM 使用
    ProviderSpec(
        name="groq",
        keywords=("groq",),
        env_key="GROQ_API_KEY",
        display_name="Groq",
        litellm_prefix="groq",              # llama3-8b-8192 → groq/llama3-8b-8192
        skip_prefixes=("groq/",),           # 避免重复前缀
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
    ),
)


# ---------------------------------------------------------------------------
# 查找辅助函数 —— 提供根据模型名、网关信息、名称查找 ProviderSpec 的能力
# ---------------------------------------------------------------------------

def find_by_model(model: str) -> ProviderSpec | None:
    """
    根据模型名关键词匹配标准服务商（大小写不敏感）。

    跳过网关和本地部署类型 —— 它们通过 api_key/api_base 而非模型名匹配。

    匹配逻辑：遍历 PROVIDERS 中的每个 spec，检查模型名是否包含 spec.keywords 中的任一关键词。
    由于 PROVIDERS 是有序的，第一个匹配的 spec 将被返回（顺序即优先级）。

    参数：
        model: 模型名称（如 "deepseek-chat"、"qwen-max"）

    返回：
        匹配的 ProviderSpec，如果没有匹配则返回 None
    """
    model_lower = model.lower()
    for spec in PROVIDERS:
        if spec.is_gateway or spec.is_local:
            continue  # 跳过网关和本地部署
        if any(kw in model_lower for kw in spec.keywords):
            return spec
    return None


def find_gateway(
    provider_name: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
) -> ProviderSpec | None:
    """
    检测当前是否通过网关或本地部署访问 LLM。

    检测优先级：
      1. provider_name —— 如果配置 key 名直接映射到网关/本地 spec，直接使用
      2. api_key 前缀 —— 如 "sk-or-" 开头 → OpenRouter
      3. api_base 关键词 —— 如 URL 中包含 "aihubmix" → AiHubMix

    注意：使用自定义 api_base 的标准服务商（如 DeepSeek 通过代理访问）
    不会被误判为 vLLM —— 旧版的兜底逻辑已被移除。

    参数：
        provider_name: 配置文件中的提供者名称（如 "openrouter"、"vllm"）
        api_key: API 密钥
        api_base: API 基础 URL

    返回：
        匹配的网关/本地 ProviderSpec，如果不是网关/本地则返回 None
    """
    # 优先级1：根据配置 key 名直接匹配
    if provider_name:
        spec = find_by_name(provider_name)
        if spec and (spec.is_gateway or spec.is_local):
            return spec

    # 优先级2：通过 api_key 前缀或 api_base 关键词自动检测
    for spec in PROVIDERS:
        if spec.detect_by_key_prefix and api_key and api_key.startswith(spec.detect_by_key_prefix):
            return spec
        if spec.detect_by_base_keyword and api_base and spec.detect_by_base_keyword in api_base:
            return spec

    return None


def find_by_name(name: str) -> ProviderSpec | None:
    """
    根据配置字段名查找 ProviderSpec。

    参数：
        name: 配置字段名（如 "dashscope"、"openrouter"）

    返回：
        匹配的 ProviderSpec，如果没有找到则返回 None
    """
    for spec in PROVIDERS:
        if spec.name == name:
            return spec
    return None
