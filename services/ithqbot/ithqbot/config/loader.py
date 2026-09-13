"""Configuration loading utilities."""

import json
import os
from pathlib import Path

from ithqbot.config.schema import Config, BootstrapConfig, ConfigStoreType
from ithqbot.config.store import ConfigStore, FileConfigStore, RedisConfigStore, SqlConfigStore
from ithqbot.utils.crypto import ConfigEncryptor, get_encryptor


# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


class ConfigValidationError(ValueError):
    """Raised when startup-sensitive configuration is invalid."""


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    
    # Try .env or default location
    return Path.cwd() / "config.json" if (Path.cwd() / "config.json").exists() else Path.home() / ".ithqbot" / "config.json"


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _format_issue(message: str) -> str:
    return f"[sensitive-config] {message}"


def _infer_provider_name_from_model(config: Config, model: str) -> str | None:
    from ithqbot.providers.registry import PROVIDERS

    forced = str(config.agents.defaults.provider or "auto").strip().lower()
    if forced and forced != "auto":
        return forced

    model_lower = str(model or "").strip().lower()
    model_normalized = model_lower.replace("-", "_")
    model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
    normalized_prefix = model_prefix.replace("-", "_")

    for spec in PROVIDERS:
        if normalized_prefix and normalized_prefix == spec.name:
            return spec.name

    for spec in PROVIDERS:
        for keyword in spec.keywords:
            kw = keyword.lower()
            if kw in model_lower or kw.replace("-", "_") in model_normalized:
                return spec.name
    return None


def _collect_sensitive_config_issues(config: Config) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    from ithqbot.providers.registry import find_by_name

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model) or _infer_provider_name_from_model(config, model)
    provider = config.get_provider(model) if config.get_provider_name(model) else None
    if provider is None and provider_name:
        provider = getattr(config.providers, provider_name, None)
    spec = find_by_name(provider_name) if provider_name else None
    provider_label = provider_name or "unknown"
    provider_api_key = str(getattr(provider, "api_key", "") or "").strip()

    if provider_name == "azure_openai":
        if not provider_api_key:
            errors.append(
                f"Active model `{model}` uses provider `{provider_label}` but `providers.azureOpenAI.apiKey` is empty."
            )
    elif (
        provider_name
        and not model.startswith("bedrock/")
        and spec is not None
        and not spec.is_oauth
        and not spec.is_local
        and not provider_api_key
    ):
        errors.append(
            f"Active model `{model}` uses provider `{provider_label}` but its `apiKey` is empty."
        )

    search = config.tools.web.search
    search_provider = str(search.provider or "").strip().lower()
    if search_provider in {"brave", "tavily", "jina"} and not str(search.api_key or "").strip():
        warnings.append(
            f"Web search provider `{search_provider}` is configured but `tools.web.search.apiKey` is empty."
        )

    minio = config.get_active_storage_config()
    access_key = str(minio.access_key or "").strip()
    secret_key = str(minio.secret_key or "").strip()
    if bool(access_key) ^ bool(secret_key):
        errors.append(
            "Object storage credentials are incomplete: configure both accessKey and secretKey under `tools.storage` or legacy `tools.minio`."
        )
    elif minio.endpoint and minio.endpoint != "localhost:9000" and not access_key and not secret_key:
        warnings.append(
            f"Object storage endpoint `{minio.endpoint}` is configured but both `accessKey` and `secretKey` are empty."
        )

    kafka = config.get_active_kafka_config()
    security_protocol = str(kafka.security_protocol or "").strip().upper()
    if "SASL" in security_protocol:
        missing: list[str] = []
        if not str(kafka.username or "").strip():
            missing.append("username")
        if not str(kafka.password or "").strip():
            missing.append("password")
        if missing:
            errors.append(
                "Kafka SASL auth is enabled via "
                f"`infrastructure.kafka.securityProtocol={security_protocol}` but missing "
                + ", ".join(f"`infrastructure.kafka.{item}`" for item in missing)
                + "."
            )

    return errors, warnings


def _validate_sensitive_config(config: Config, *, strict: bool = False) -> None:
    errors, warnings = _collect_sensitive_config_issues(config)

    for message in warnings:
        print(f"Warning: {_format_issue(message)}")

    for message in errors:
        print(f"Warning: {_format_issue(message)}")

    if strict and errors:
        joined = "\n".join(f"- {message}" for message in errors)
        raise ConfigValidationError(
            "Sensitive configuration validation failed:\n" + joined
        )


def load_config(
    config_path: Path | None = None,
    *,
    bot_id: str | None = None,
    strict_sensitive_config: bool | None = None,
) -> Config:
    """
    Load configuration from store or create default.

    Args:
        config_path: Optional path to config file (used for bootstrap).
        bot_id: Optional bot ID to load. Overrides bootstrap bot_id.
        strict_sensitive_config: Whether to fail on validation errors.

    Returns:
        Loaded configuration object.
    """
    # 1. Load Bootstrap Config
    path = config_path or get_config_path()
    bootstrap_data = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                bootstrap_data = json.load(f)
        except Exception:
            pass
    
    bootstrap = BootstrapConfig(**bootstrap_data)
    active_bot_id = bot_id or bootstrap.bot_id
    strict = _env_flag("ITHQBOT_STRICT_CONFIG") if strict_sensitive_config is None else strict_sensitive_config

    # 2. Initialize Encryptor
    encryptor: ConfigEncryptor | None = None
    if bootstrap.encryption_enabled and bootstrap.encryption_key:
        encryptor = get_encryptor(bootstrap.encryption_algorithm, bootstrap.encryption_key)

    # 3. Initialize Store
    store: ConfigStore
    if bootstrap.config_store_type == ConfigStoreType.REDIS and bootstrap.config_store_uri:
        store = RedisConfigStore(bootstrap.config_store_uri, encryptor=encryptor)
    elif bootstrap.config_store_type == ConfigStoreType.SQL and bootstrap.config_store_uri:
        store = SqlConfigStore(bootstrap.config_store_uri, encryptor=encryptor)
    else:
        # Default to file store using the parent of config_path or ~/.ithqbot
        store = FileConfigStore(path.parent, encryptor=encryptor)

    # 4. Load from Store
    data = store.load(active_bot_id)
    if data:
        try:
            data = _migrate_config(data)
            config = Config.model_validate(data)
            _validate_sensitive_config(config, strict=strict)
            return config
        except ConfigValidationError:
            raise
        except Exception as e:
            print(f"Warning: Failed to validate config for {active_bot_id}: {e}")
            print("Using default configuration.")

    config = Config()
    # Ensure bot ID matches
    if active_bot_id and active_bot_id != "default":
        config.bot.id = active_bot_id
        
    _validate_sensitive_config(config, strict=strict)
    return config


def get_config_store(config_path: Path | None = None) -> ConfigStore:
    """Helper to get the active config store."""
    path = config_path or get_config_path()
    bootstrap_data = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                bootstrap_data = json.load(f)
        except Exception:
            pass
    bootstrap = BootstrapConfig(**bootstrap_data)
    
    encryptor: ConfigEncryptor | None = None
    if bootstrap.encryption_enabled and bootstrap.encryption_key:
        encryptor = get_encryptor(bootstrap.encryption_algorithm, bootstrap.encryption_key)

    if bootstrap.config_store_type == ConfigStoreType.REDIS and bootstrap.config_store_uri:
        return RedisConfigStore(bootstrap.config_store_uri, encryptor=encryptor)
    if bootstrap.config_store_type == ConfigStoreType.SQL and bootstrap.config_store_uri:
        return SqlConfigStore(bootstrap.config_store_uri, encryptor=encryptor)
    return FileConfigStore(path.parent, encryptor=encryptor)


def save_config(config: Config, config_path: Path | None = None, bot_id: str | None = None) -> None:
    """
    Save configuration to store.

    Args:
        config: Configuration to save.
        config_path: Optional path to bootstrap config.
        bot_id: Optional bot ID to save. Uses config.bot.id if not provided.
    """
    store = get_config_store(config_path)
    active_bot_id = bot_id or config.bot.id
    data = config.model_dump(by_alias=True)
    store.save(active_bot_id, data)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
