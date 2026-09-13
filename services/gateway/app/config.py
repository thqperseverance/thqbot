"""运行配置：全部来自环境变量（前缀 ``APP_``）。

对应 ``.env`` / docker-compose 中的 ``APP_*`` 变量。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 基础设施 ----
    database_url: str = "postgresql+psycopg://thqbot:thqbot_dev_pw@localhost:5432/thqbot"
    redis_uri: str = "redis://localhost:6379/0"

    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_inbound_topic: str = "icatmsg_inbound"
    kafka_outbound_topic: str = "icatmsg_outbound"
    kafka_outbound_group: str = "thqbot-gateway"
    kafka_enabled: bool = True

    # ---- 会话/鉴权 ----
    session_secret: str = "dev-session-secret-change-me"
    session_cookie: str = "thqbot_session"
    session_max_age: int = 7 * 24 * 3600
    # 可选：JSON 数组种子用户，形如 [{"username":"admin","password":"...","display_name":"..."}]
    auth_users: str = ""

    # ---- agent 协议 ----
    agent_tenant_id: str = "thqbot"
    agent_bot_id: str = "bot_A"
    client_id: str = "thqbot-web"
    # agent 工作区是否受限（仅用于前端展示"权限提示"，不影响 worker 真实行为）
    agent_workspace_restricted: bool = True

    # ---- LLM（仅用于前端展示模型名；真实调用在 worker 侧） ----
    llm_model: str = "deepseek-flash"
    # 可选：逗号分隔的模型清单，前端下拉展示；留空则只有 llm_model
    llm_models: str = ""

    # ---- 实时推送 ----
    sse_heartbeat_seconds: float = 15.0
    sse_queue_size: int = 256

    # ---- 缓存/去重 TTL（D7=A：Redis 只做缓存/通道/锁） ----
    dedupe_ttl_seconds: int = 7 * 24 * 3600
    status_cache_ttl_seconds: int = 3600

    # ---- 对象存储（附件） ----
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "ithqbot-storage"
    minio_secure: bool = False
    max_upload_bytes: int = 20 * 1024 * 1024
    max_attachments_per_message: int = 10

    # ---- 前端静态资源 ----
    web_dist_dir: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
