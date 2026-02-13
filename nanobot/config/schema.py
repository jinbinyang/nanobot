"""
配置数据模型定义 (config/schema.py)
=================================
本模块使用 Pydantic 定义 nanobot 的完整配置结构。
所有配置项都有默认值，用户只需在 config.json 中覆盖需要修改的部分。

整体配置结构（树形）：
Config (根配置)
├── agents        - Agent 智能体相关配置（模型、温度、工具迭代次数等）
├── channels      - 消息渠道配置（Telegram/Discord/飞书/钉钉/WhatsApp/Email/Slack/QQ/Mochat）
├── providers     - LLM 提供商配置（API Key、API Base URL 等）
├── gateway       - HTTP 网关服务配置（主机和端口）
└── tools         - 工具配置（Web 搜索、Shell 执行等）

对于 Java 开发者：
- Pydantic 的 BaseModel 类似于 Java 的 POJO/Record，但自带字段验证和默认值
- BaseSettings 类似于 Spring 的 @ConfigurationProperties，额外支持从环境变量读取配置
- Field(default_factory=...) 类似于 Java 中用工厂方法创建可变默认值，避免共享引用问题
"""

from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from pydantic_settings import BaseSettings


# ==============================================================================
# 渠道配置模型（各消息平台的连接参数）
# 每个渠道都有 enabled 开关和 allow_from 白名单，用于安全控制
# ==============================================================================


class WhatsAppConfig(BaseModel):
    """WhatsApp 渠道配置。通过 WebSocket 连接到 WhatsApp Bridge 服务。"""
    enabled: bool = False  # 是否启用该渠道
    bridge_url: str = "ws://localhost:3001"  # WhatsApp Bridge 的 WebSocket 地址
    bridge_token: str = ""  # Bridge 认证令牌（可选但推荐设置，用于安全通信）
    allow_from: list[str] = Field(default_factory=list)  # 允许的手机号码白名单


class TelegramConfig(BaseModel):
    """Telegram 渠道配置。使用 Bot API 长轮询方式接收消息。"""
    enabled: bool = False  # 是否启用该渠道
    token: str = ""  # 从 @BotFather 获取的 Bot Token
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户 ID 或用户名白名单
    proxy: str | None = None  # HTTP/SOCKS5 代理地址，如 "http://127.0.0.1:7890"


class FeishuConfig(BaseModel):
    """飞书/Lark 渠道配置。使用 WebSocket 长连接接收事件。"""
    enabled: bool = False
    app_id: str = ""  # 飞书开放平台的 App ID
    app_secret: str = ""  # 飞书开放平台的 App Secret
    encrypt_key: str = ""  # 事件订阅的加密密钥（可选）
    verification_token: str = ""  # 事件订阅的验证令牌（可选）
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户 open_id 白名单


class DingTalkConfig(BaseModel):
    """钉钉渠道配置。使用 Stream 模式接收消息（无需公网回调地址）。"""
    enabled: bool = False
    client_id: str = ""  # 钉钉应用的 AppKey
    client_secret: str = ""  # 钉钉应用的 AppSecret
    allow_from: list[str] = Field(default_factory=list)  # 允许的员工 staff_id 白名单


class DiscordConfig(BaseModel):
    """Discord 渠道配置。使用 Gateway WebSocket 接收消息。"""
    enabled: bool = False
    token: str = ""  # 从 Discord Developer Portal 获取的 Bot Token
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户 ID 白名单
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"  # Discord Gateway 地址
    intents: int = 37377  # Gateway Intents 位掩码: GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT

class EmailConfig(BaseModel):
    """
    邮件渠道配置。
    入站使用 IMAP 协议轮询收件箱，出站使用 SMTP 协议发送回复。
    """
    enabled: bool = False
    consent_granted: bool = False  # 必须显式授权才能访问邮箱数据（隐私合规要求）

    # IMAP 收件配置
    imap_host: str = ""  # IMAP 服务器地址
    imap_port: int = 993  # IMAP 端口（993 为 SSL 默认端口）
    imap_username: str = ""  # IMAP 登录用户名
    imap_password: str = ""  # IMAP 登录密码（或应用专用密码）
    imap_mailbox: str = "INBOX"  # 监控的邮箱文件夹
    imap_use_ssl: bool = True  # 是否使用 SSL 加密

    # SMTP 发件配置
    smtp_host: str = ""  # SMTP 服务器地址
    smtp_port: int = 587  # SMTP 端口（587 为 STARTTLS 默认端口）
    smtp_username: str = ""  # SMTP 登录用户名
    smtp_password: str = ""  # SMTP 登录密码
    smtp_use_tls: bool = True  # 是否使用 STARTTLS
    smtp_use_ssl: bool = False  # 是否使用 SSL（与 TLS 二选一）
    from_address: str = ""  # 发件人地址

    # 行为控制
    auto_reply_enabled: bool = True  # 是否自动回复（False 时只读取不回复）
    poll_interval_seconds: int = 30  # 轮询间隔（秒）
    mark_seen: bool = True  # 处理后是否标记为已读
    max_body_chars: int = 12000  # 邮件正文最大字符数（防止过长邮件）
    subject_prefix: str = "Re: "  # 回复邮件的主题前缀
    allow_from: list[str] = Field(default_factory=list)  # 允许的发件人邮箱白名单


# ==============================================================================
# Mochat 相关配置（较复杂，有多个子配置类）
# Mochat 是 OpenClaw 生态中的聊天平台
# ==============================================================================


class MochatMentionConfig(BaseModel):
    """Mochat @提及行为配置。"""
    require_in_groups: bool = False  # 在群组中是否要求 @机器人才响应


class MochatGroupRule(BaseModel):
    """Mochat 单个群组的提及规则。"""
    require_mention: bool = False  # 该群组是否要求 @提及才响应


class MochatConfig(BaseModel):
    """
    Mochat 渠道配置。
    Mochat 是 OpenClaw 生态的聊天界面，通过 Socket.IO 实现实时通信。
    配置项较多，包括连接参数、重试策略、消息延迟等。
    """
    enabled: bool = False
    base_url: str = "https://mochat.io"  # Mochat 服务基础 URL
    socket_url: str = ""  # Socket.IO 连接地址（为空时使用 base_url）
    socket_path: str = "/socket.io"  # Socket.IO 路径
    socket_disable_msgpack: bool = False  # 是否禁用 msgpack 序列化
    socket_reconnect_delay_ms: int = 1000  # 重连初始延迟（毫秒）
    socket_max_reconnect_delay_ms: int = 10000  # 重连最大延迟（毫秒，指数退避上限）
    socket_connect_timeout_ms: int = 10000  # 连接超时（毫秒）
    refresh_interval_ms: int = 30000  # 数据刷新间隔（毫秒）
    watch_timeout_ms: int = 25000  # 长轮询超时（毫秒）
    watch_limit: int = 100  # 每次轮询获取的消息数量上限
    retry_delay_ms: int = 500  # 操作重试延迟（毫秒）
    max_retry_attempts: int = 0  # 最大重试次数（0 表示无限重试）
    claw_token: str = ""  # Mochat 认证令牌
    agent_user_id: str = ""  # 机器人在 Mochat 中的用户 ID
    sessions: list[str] = Field(default_factory=list)  # 监控的会话 ID 列表
    panels: list[str] = Field(default_factory=list)  # 监控的面板 ID 列表
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户 ID 白名单
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)  # @提及行为配置
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)  # 每个群组的独立规则
    reply_delay_mode: str = "non-mention"  # 回复延迟模式: off（关闭）| non-mention（非@时延迟）
    reply_delay_ms: int = 120000  # 回复延迟时间（毫秒，约2分钟）


class SlackDMConfig(BaseModel):
    """Slack 私聊（DM）策略配置。"""
    enabled: bool = True  # 是否接受私聊消息
    policy: str = "open"  # 策略: "open"（所有人）| "allowlist"（仅白名单）
    allow_from: list[str] = Field(default_factory=list)  # 允许的 Slack 用户 ID 白名单


class SlackConfig(BaseModel):
    """Slack 渠道配置。使用 Socket Mode 接收事件（无需公网回调）。"""
    enabled: bool = False
    mode: str = "socket"  # 连接模式，目前仅支持 "socket"
    webhook_path: str = "/slack/events"  # Webhook 路径（保留字段）
    bot_token: str = ""  # Bot Token (xoxb-...)
    app_token: str = ""  # App-Level Token (xapp-...)，Socket Mode 必需
    user_token_read_only: bool = True  # 用户令牌是否只读
    group_policy: str = "mention"  # 群组消息策略: "mention"（@时响应）| "open" | "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # 允许的频道 ID（allowlist 模式下）
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)  # 私聊策略配置


class QQConfig(BaseModel):
    """QQ 渠道配置。使用 QQ 官方 botpy SDK 接入。"""
    enabled: bool = False
    app_id: str = ""  # 机器人 AppID，从 q.qq.com 获取
    secret: str = ""  # 机器人 AppSecret，从 q.qq.com 获取
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户 openid 白名单


# ==============================================================================
# 聚合配置类 —— 将所有渠道配置汇总到一个类中
# ==============================================================================


class ChannelsConfig(BaseModel):
    """所有消息渠道的聚合配置。每个渠道都是可选的，默认全部关闭。"""
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    mochat: MochatConfig = Field(default_factory=MochatConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    qq: QQConfig = Field(default_factory=QQConfig)


# ==============================================================================
# Agent 智能体配置
# ==============================================================================


class AgentDefaults(BaseModel):
    """
    Agent 默认配置。定义了智能体的核心运行参数。

    对于 Java 开发者：
    - model: 类似于指定使用哪个数据库驱动，这里是指定使用哪个 LLM 模型
    - max_tool_iterations: 防止无限循环的安全阀，类似于递归深度限制
    - memory_window: 对话上下文窗口大小，类似于缓存的最大条目数
    """
    workspace: str = "~/.nanobot/workspace"  # Agent 工作区目录（文件读写操作的根目录）
    model: str = "anthropic/claude-opus-4-5"  # 默认使用的 LLM 模型（格式: provider/model）
    max_tokens: int = 8192  # 单次 LLM 调用的最大输出 token 数
    temperature: float = 0.7  # 生成温度（0-1，越高越随机/创意，越低越确定/保守）
    max_tool_iterations: int = 20  # 单次对话中允许的最大工具调用轮次（防止死循环）
    memory_window: int = 50  # 对话历史窗口大小（保留最近 N 条消息作为上下文）


class AgentsConfig(BaseModel):
    """Agent 配置容器。目前只有 defaults，未来可扩展为支持多个不同配置的 Agent。"""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


# ==============================================================================
# LLM 提供商配置
# ==============================================================================


class ProviderConfig(BaseModel):
    """
    单个 LLM 提供商的配置。

    nanobot 通过 LiteLLM 统一适配层支持多家 LLM 提供商，
    每个提供商只需配置 api_key 即可使用。
    """
    api_key: str = ""  # API 密钥（留空表示未配置该提供商）
    api_base: str | None = None  # 自定义 API 基础 URL（用于私有部署或代理）
    extra_headers: dict[str, str] | None = None  # 额外请求头（如 AiHubMix 的 APP-Code）


class ProvidersConfig(BaseModel):
    """
    所有 LLM 提供商的聚合配置。

    支持的提供商列表（用户只需配置使用的那个）：
    - anthropic: Anthropic Claude 系列
    - openai: OpenAI GPT 系列
    - openrouter: OpenRouter 聚合网关（可访问多家模型）
    - deepseek: DeepSeek 深度求索
    - groq: Groq 高速推理（也提供 Whisper 语音转录）
    - zhipu: 智谱 AI（GLM 系列）
    - dashscope: 阿里云通义千问（DashScope API）
    - vllm: vLLM 本地部署推理
    - gemini: Google Gemini
    - moonshot: Moonshot AI（月之暗面/Kimi）
    - minimax: MiniMax
    - aihubmix: AiHubMix API 聚合网关
    """
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里云通义千问
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API 聚合网关


# ==============================================================================
# 其他配置
# ==============================================================================


class GatewayConfig(BaseModel):
    """HTTP 网关服务配置（用于接收 Webhook 回调等）。"""
    host: str = "0.0.0.0"  # 监听地址（0.0.0.0 表示监听所有网卡）
    port: int = 18790  # 监听端口


class WebSearchConfig(BaseModel):
    """Web 搜索工具配置。使用 Brave Search API 提供搜索能力。"""
    api_key: str = ""  # Brave Search API 密钥
    max_results: int = 5  # 每次搜索返回的最大结果数


class WebToolsConfig(BaseModel):
    """Web 相关工具的聚合配置。"""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(BaseModel):
    """Shell 命令执行工具配置。"""
    timeout: int = 60  # 命令执行超时时间（秒）


class ToolsConfig(BaseModel):
    """
    工具总配置。

    restrict_to_workspace: 安全沙箱开关
    - True: 所有文件/Shell 操作都限制在 workspace 目录内（生产环境推荐）
    - False: 允许访问整个文件系统（开发环境默认）
    """
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # 是否限制工具访问范围到工作区目录


# ==============================================================================
# 根配置类 —— 整个 nanobot 的配置入口
# ==============================================================================


class Config(BaseSettings):
    """
    nanobot 根配置类。

    继承自 Pydantic 的 BaseSettings，除了支持从 JSON 文件加载外，
    还支持从环境变量读取配置：
    - 环境变量前缀: NANOBOT_
    - 嵌套分隔符: __ (双下划线)
    - 示例: NANOBOT_AGENTS__DEFAULTS__MODEL=openai/gpt-4 可覆盖 agents.defaults.model

    对于 Java 开发者：
    - 类似 Spring Boot 的 @ConfigurationProperties + 环境变量覆盖机制
    - 如 application.yml 中的值可以被 SPRING_DATASOURCE_URL 环境变量覆盖
    """
    agents: AgentsConfig = Field(default_factory=AgentsConfig)  # Agent 智能体配置
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)  # 消息渠道配置
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)  # LLM 提供商配置
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)  # HTTP 网关配置
    tools: ToolsConfig = Field(default_factory=ToolsConfig)  # 工具配置
    
    @property
    def workspace_path(self) -> Path:
        """获取展开后的工作区绝对路径（将 ~ 展开为用户主目录）。"""
        return Path(self.agents.defaults.workspace).expanduser()
    
    def _match_provider(self, model: str | None = None) -> tuple["ProviderConfig | None", str | None]:
        """
        根据模型名称匹配对应的 LLM 提供商配置。

        匹配策略（两阶段）：
        1. 关键词匹配：根据模型名中的关键词（如 "claude" → anthropic, "gpt" → openai）
           找到对应的提供商，且该提供商必须已配置 api_key
        2. 兜底匹配：如果关键词匹配失败，返回第一个已配置 api_key 的提供商
           （优先返回网关类提供商，如 openrouter/aihubmix）

        参数:
            model: 模型名称（如 "anthropic/claude-sonnet-4"），为 None 时使用默认模型

        返回:
            (提供商配置, 提供商名称) 的元组，均可能为 None
        """
        from nanobot.providers.registry import PROVIDERS
        model_lower = (model or self.agents.defaults.model).lower()

        # 第一阶段：按关键词精确匹配（遵循 PROVIDERS 注册表的顺序）
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(kw in model_lower for kw in spec.keywords) and p.api_key:
                return p, spec.name

        # 第二阶段：兜底策略 —— 返回第一个有 api_key 的提供商
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """获取匹配的提供商配置（包含 api_key、api_base、extra_headers）。"""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """获取匹配的提供商注册名称（如 "deepseek"、"openrouter"）。"""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """获取指定模型对应的 API Key。未匹配到提供商时返回 None。"""
        p = self.get_provider(model)
        return p.api_key if p else None
    
    def get_api_base(self, model: str | None = None) -> str | None:
        """
        获取指定模型对应的 API Base URL。

        优先级：
        1. 用户在 config.json 中显式配置的 api_base
        2. 网关类提供商的默认 api_base（如 openrouter 的 https://openrouter.ai/api/v1）
        3. 标准提供商不返回默认 api_base（它们通过环境变量在 _setup_env 中设置，
           避免污染 litellm 的全局 api_base）
        """
        from nanobot.providers.registry import find_by_name
        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # 仅网关类提供商才返回默认 api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.is_gateway and spec.default_api_base:
                return spec.default_api_base
        return None
    
    # Pydantic Settings 配置：支持 NANOBOT_ 前缀的环境变量，嵌套用 __ 分隔
    model_config = ConfigDict(
        env_prefix="NANOBOT_",  # 环境变量前缀
        env_nested_delimiter="__"  # 嵌套配置的分隔符
    )
