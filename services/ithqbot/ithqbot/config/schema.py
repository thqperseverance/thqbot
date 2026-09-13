"""Configuration schema using Pydantic."""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RoutePurpose(StrEnum):
    """Supported routing purposes."""

    CLASSIFIER = "classifier"
    PLANNER = "planner"
    FINAL_ANSWER = "final_answer"
    EXTRACTION = "extraction"
    VISION = "vision"


class ConfigStoreType(StrEnum):
    """Supported configuration storage types."""

    FILE = "file"
    REDIS = "redis"
    SQL = "sql"


class RouteProfile(Base):
    """Normalized route result returned by the model router."""

    purpose: RoutePurpose = RoutePurpose.PLANNER
    active_model: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    source: str = "default"
    tier: Literal["small", "medium", "large"] | None = None

    @property
    def model(self) -> str:
        """Backward-compatible alias for older callers."""
        return self.active_model


class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.ithqbot/workspace"
    session_store_uri: str | None = None  # e.g., "redis://localhost:6379/0" or "file:///path/to/sessions"
    memory_store_uri: str | None = "postgresql://127.0.0.1:5432/ithqbot"
    cron_store_uri: str | None = "postgresql://127.0.0.1:5432/ithqbot"
    require_external_session_store: bool = True
    require_external_memory_store: bool = True
    require_external_cron_store: bool = True
    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    max_tool_iterations: int = 40
    max_tool_concurrency: int = 5
    # Deprecated compatibility field: accepted from old configs but ignored at runtime.
    memory_window: int | None = Field(default=None, exclude=True)
    reasoning_effort: str | None = None  # low / medium / high — enables LLM thinking mode

    @property
    def should_warn_deprecated_memory_window(self) -> bool:
        """Return True when old memoryWindow is present without contextWindowTokens."""
        return self.memory_window is not None and "context_window_tokens" not in self.model_fields_set


class RoutingRule(Base):
    """Rule-based routing configuration."""

    name: str = ""
    keywords: list[str] = Field(default_factory=list)
    purpose: RoutePurpose | None = None
    tier: Literal["small", "medium", "large"] = "medium"
    active_model: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    max_tokens: int | None = None
    reasoning_effort: str | None = None


class TierModelConfig(Base):
    """Model and generation settings for one routing tier."""

    model: str = ""
    max_tokens: int | None = None
    reasoning_effort: str | None = None


class RoutingTiers(Base):
    """Tier model mapping."""

    small: TierModelConfig = Field(default_factory=TierModelConfig)
    medium: TierModelConfig = Field(default_factory=TierModelConfig)
    large: TierModelConfig = Field(default_factory=TierModelConfig)


class RoutingFallbacks(Base):
    """Fallback chain for each tier."""

    small: list[Literal["small", "medium", "large"]] = Field(default_factory=lambda: ["medium", "large"])
    medium: list[Literal["small", "medium", "large"]] = Field(default_factory=lambda: ["large"])
    large: list[Literal["small", "medium", "large"]] = Field(default_factory=list)


class RoutingThresholds(Base):
    """Threshold configuration for classifier outputs."""

    confidence_min: float = 0.72


class RoutingOverride(Base):
    """Per-tenant/per-bot routing override."""

    name: str = ""
    tenant_id: str = ""
    bot_id: str = ""
    purpose: RoutePurpose | None = None
    forced_tier: Literal["small", "medium", "large"] | None = None
    forced_model: str = ""
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    fallback_models: list[str] = Field(default_factory=list)


class RoutingMatrixEntry(Base):
    """Purpose+tier specific routing settings."""

    active_model: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    max_tokens: int | None = None
    reasoning_effort: str | None = None


class RoutingPurposeMatrix(Base):
    """Routing entries for one purpose across all tiers."""

    small: RoutingMatrixEntry = Field(default_factory=RoutingMatrixEntry)
    medium: RoutingMatrixEntry = Field(default_factory=RoutingMatrixEntry)
    large: RoutingMatrixEntry = Field(default_factory=RoutingMatrixEntry)

    @model_validator(mode="before")
    @classmethod
    def _expand_all_tiers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        shared = value.get("all_tiers")
        if shared is None:
            shared = value.get("allTiers")
        if not isinstance(shared, dict):
            return value
        expanded: dict[str, Any] = dict(value)
        expanded.pop("all_tiers", None)
        expanded.pop("allTiers", None)
        for tier in ("small", "medium", "large"):
            tier_value = expanded.get(tier)
            if isinstance(tier_value, dict):
                expanded[tier] = {**shared, **tier_value}
            else:
                expanded[tier] = dict(shared)
        return expanded


class RoutingMatrix(Base):
    """Full routing matrix keyed by purpose and tier."""

    classifier: RoutingPurposeMatrix = Field(default_factory=RoutingPurposeMatrix)
    planner: RoutingPurposeMatrix = Field(default_factory=RoutingPurposeMatrix)
    final_answer: RoutingPurposeMatrix = Field(default_factory=RoutingPurposeMatrix)
    extraction: RoutingPurposeMatrix = Field(default_factory=RoutingPurposeMatrix)
    vision: RoutingPurposeMatrix = Field(default_factory=RoutingPurposeMatrix)


def _default_route_profile(
    *,
    purpose: RoutePurpose,
    active_model: str = "",
    fallback_models: list[str] | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> RouteProfile:
    return RouteProfile(
        purpose=purpose,
        active_model=active_model,
        fallback_models=list(fallback_models or []),
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        source="default",
    )


class RoutingProfiles(Base):
    """Purpose-level routing defaults."""

    classifier: RouteProfile = Field(
        default_factory=lambda: _default_route_profile(
            purpose=RoutePurpose.CLASSIFIER,
        )
    )
    planner: RouteProfile = Field(
        default_factory=lambda: _default_route_profile(
            purpose=RoutePurpose.PLANNER,
        )
    )
    final_answer: RouteProfile = Field(
        default_factory=lambda: _default_route_profile(
            purpose=RoutePurpose.FINAL_ANSWER,
        )
    )
    extraction: RouteProfile = Field(
        default_factory=lambda: _default_route_profile(
            purpose=RoutePurpose.EXTRACTION,
        )
    )
    vision: RouteProfile = Field(
        default_factory=lambda: _default_route_profile(
            purpose=RoutePurpose.VISION,
        )
    )


class AgentRoutingConfig(Base):
    """Two-stage model routing configuration."""

    enabled: bool = False
    router_model: str = ""
    classifier_temperature: float = 0.0
    classifier_max_tokens: int = 256
    matrix: RoutingMatrix = Field(default_factory=RoutingMatrix)
    thresholds: RoutingThresholds = Field(default_factory=RoutingThresholds)
    profiles: RoutingProfiles = Field(default_factory=RoutingProfiles)
    tiers: RoutingTiers = Field(default_factory=RoutingTiers)
    fallbacks: RoutingFallbacks = Field(default_factory=RoutingFallbacks)
    rules: list[RoutingRule] = Field(default_factory=list)
    overrides: list[RoutingOverride] = Field(default_factory=list)


class BotGuardrailsPolicy(Base):
    """Per-bot guardrails policy."""

    enabled: bool = True
    tool_allowlist: list[str] = Field(default_factory=list)
    tool_denylist: list[str] = Field(default_factory=list)
    blocked_instruction_patterns: list[str] = Field(default_factory=list)
    blocked_instruction_message: str = "请求触发安全策略，已拦截。"
    sensitive_words: list[str] = Field(default_factory=list)
    sensitive_word_message: str = "输出包含敏感内容，已拦截。"


class BotGuardrailsConfig(Base):
    """Bot-level skill and safety guardrails configuration."""

    enabled: bool = False
    history_limit: int = 500
    default_policy: BotGuardrailsPolicy = Field(default_factory=BotGuardrailsPolicy)
    bots: dict[str, BotGuardrailsPolicy] = Field(default_factory=dict)


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    routing: AgentRoutingConfig = Field(default_factory=AgentRoutingConfig)
    embedding_model: str = "openai/text-embedding-3-small"
    available_models: list[str] = Field(default_factory=list)




class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    model_api_bases: dict[str, str] = Field(default_factory=dict)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    nvidia: ProviderConfig = Field(default_factory=ProviderConfig)  # NVIDIA NIA
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)  # Github Copilot (OAuth)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    lease_enabled: bool = True
    lease_ttl_s: int = 5 * 60


class ObservabilityConfig(Base):
    """Observability and tracing configuration."""

    enabled: bool = True
    async_mode: bool = True  # Async reporting via queue
    queue_size: int = 2000
    batch_size: int = 100
    flush_interval_s: float = 1.0  # seconds between flushes
    trace_sample_rate: float = 1.0
    trace_noise_suppression_window_ms: int = 1000
    noisy_trace_events: list[str] = Field(default_factory=lambda: ["skill.progress", "kafka.inbound.consumed"])
    trace_sample_exempt_events: list[str] = Field(
        default_factory=lambda: ["agent.task.error", "agent.task.cancelled", "agent.task.completed", "server.client.delivered"]
    )

    # Retention
    redis_retention_h: int = 24  # Cache retention (hours)
    pg_retention_d: int = 3      # Persistence retention (days)

    # Persistence (PostgreSQL)
    pg_uri: str | None = None
    admin_account_ids: list[str] = Field(default_factory=list)


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)





class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools

class ObjectStorageConfig(Base):
    """Object storage configuration for MinIO or S3 backends."""

    backend: Literal["minio", "s3"] = "minio"
    endpoint: str = "localhost:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "ithqbot-storage"
    secure: bool = False
    region: str = ""


class MinioConfig(ObjectStorageConfig):
    """Backward-compatible alias for legacy MinIO configuration."""


class FileApiConfig(Base):
    """Configuration for tenant-isolated file service."""

    max_file_size_bytes: int = 10 * 1024 * 1024
    download_url_ttl_seconds: int = 600
    metadata_prefix: str = "_meta/files"

class RedisInfrastructureConfig(Base):
    """Shared Redis infrastructure configuration."""

    uri: str = ""


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    storage: ObjectStorageConfig = Field(default_factory=ObjectStorageConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    file_api: FileApiConfig = Field(default_factory=FileApiConfig)

    enabled: list[str] = Field(default_factory=lambda: ["*"])
    disabled: list[str] = Field(default_factory=list)
    active_storage_profile: str | None = None
    storage_profiles: dict[str, ObjectStorageConfig] = Field(default_factory=dict)
    active_minio_profile: str | None = None
    minio_profiles: dict[str, MinioConfig] = Field(default_factory=dict)
    file_root: str | None = None
    minio_workspace: str | None = None
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class BotSkillsPolicy(Base):
    """Per-bot skills policy."""

    enabled_skills: list[str] = Field(default_factory=list)


class SkillsConfig(Base):
    """Global and per-bot skills configuration."""

    enabled_skills: list[str] = Field(default_factory=list)
    bots: dict[str, BotSkillsPolicy] = Field(default_factory=dict)



class KafkaInfrastructureConfig(Base):
    """Shared Kafka infrastructure configuration."""

    servers: str = "localhost:9092"
    security_protocol: str = ""
    sasl_mechanism: str = "PLAIN"
    username: str = ""
    password: str = ""
    inbound_topic: str = "icatmsg_inbound"
    outbound_topic: str = "icatmsg_outbound"


class InfrastructureConfig(Base):
    """Shared infrastructure configuration."""

    kafka: KafkaInfrastructureConfig = Field(default_factory=KafkaInfrastructureConfig)
    redis: RedisInfrastructureConfig = Field(default_factory=RedisInfrastructureConfig)
    active_kafka_profile: str | None = None
    kafka_profiles: dict[str, KafkaInfrastructureConfig] = Field(default_factory=dict)
    active_redis_profile: str | None = None
    redis_profiles: dict[str, RedisInfrastructureConfig] = Field(default_factory=dict)



class BotIdentityConfig(Base):
    """Identity configuration for the current bot."""
    id: str = "bot_A"
    name: str = "ithqbot运维专家"
    description: str = "You are ithqbot, a helpful AI assistant."


class Config(BaseSettings):
    """Root configuration for ithqbot."""

    bot: BotIdentityConfig = Field(default_factory=BotIdentityConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    infrastructure: InfrastructureConfig = Field(default_factory=InfrastructureConfig)
    bot_guardrails: BotGuardrailsConfig = Field(default_factory=BotGuardrailsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def get_active_kafka_config(self) -> KafkaInfrastructureConfig:
        infra = self.infrastructure
        profile_name = infra.active_kafka_profile
        if profile_name and profile_name in infra.kafka_profiles:
            return infra.kafka_profiles[profile_name]
        return infra.kafka

    def get_active_storage_config(self) -> ObjectStorageConfig:
        tools = self.tools
        profile_name = tools.active_storage_profile
        if profile_name and profile_name in tools.storage_profiles:
            return tools.storage_profiles[profile_name]
        if "storage" in tools.model_fields_set:
            return tools.storage
        return self.get_active_minio_config()

    def get_active_minio_config(self) -> MinioConfig:
        tools = self.tools
        profile_name = tools.active_minio_profile
        if profile_name and profile_name in tools.minio_profiles:
            return tools.minio_profiles[profile_name]
        return tools.minio


    def get_active_session_store_uri(self) -> str | None:
        defaults = self.agents.defaults
        if defaults.session_store_uri:
            return defaults.session_store_uri

        infra = self.infrastructure
        profile_name = infra.active_redis_profile
        if profile_name and profile_name in infra.redis_profiles:
            uri = infra.redis_profiles[profile_name].uri
            return uri or None

        return infra.redis.uri or None

    def get_active_memory_store_uri(self) -> str | None:
        defaults = self.agents.defaults
        if defaults.memory_store_uri:
            return defaults.memory_store_uri
        uri = self.get_active_session_store_uri()
        return uri or None

    def get_active_cron_store_uri(self) -> str | None:
        defaults = self.agents.defaults
        if defaults.cron_store_uri:
            return defaults.cron_store_uri
        uri = self.get_active_memory_store_uri()
        return uri or None

    def get_enabled_skills(self, bot_id: str | None = None) -> list[str]:
        cfg = self.skills
        if bot_id and bot_id in cfg.bots:
            policy = cfg.bots[bot_id]
            if policy.enabled_skills:
                return list(policy.enabled_skills)
        return list(cfg.enabled_skills)

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from ithqbot.providers.registry import PROVIDERS

        forced = self.agents.defaults.provider
        if forced != "auto":
            p = getattr(self.providers, forced, None)
            return (p, forced) if p else (None, None)

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from ithqbot.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # keep their own endpoint handling isolated from shared global state.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="ITHQBOT_", env_nested_delimiter="__")


class BootstrapConfig(BaseSettings):
    """Minimal configuration required to bootstrap the full config from a store."""

    config_store_type: ConfigStoreType = ConfigStoreType.FILE
    config_store_uri: str | None = None
    bot_id: str = "default"

    # Encryption settings
    encryption_enabled: bool = False
    encryption_algorithm: str = "aes"  # "aes" or "sm4"
    encryption_key: str | None = None

    model_config = ConfigDict(env_prefix="ITHQBOT_BOOTSTRAP_", env_nested_delimiter="__", extra="ignore")
