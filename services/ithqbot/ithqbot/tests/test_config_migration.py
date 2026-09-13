import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ithqbot.cli.commands import app
from ithqbot.config.loader import ConfigValidationError, load_config, save_config
from ithqbot.config.schema import Config
from ithqbot.session.factory import _SESSION_STORE_FACTORIES, create_session_store

runner = CliRunner()


def test_load_config_keeps_max_tokens_and_warns_on_legacy_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 1234,
                        "memoryWindow": 42,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.agents.defaults.max_tokens == 1234
    assert config.agents.defaults.context_window_tokens == 65_536
    assert config.agents.defaults.should_warn_deprecated_memory_window is True


def test_save_config_writes_context_window_tokens_but_not_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 2222,
                        "memoryWindow": 30,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = saved["agents"]["defaults"]

    assert defaults["maxTokens"] == 2222
    assert defaults["contextWindowTokens"] == 65_536
    assert "memoryWindow" not in defaults


def test_onboard_refresh_rewrites_legacy_config_template(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 3333,
                        "memoryWindow": 50,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("ithqbot.config.loader.get_config_path", lambda: config_path)

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "contextWindowTokens" in result.stdout
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = saved["agents"]["defaults"]
    assert defaults["maxTokens"] == 3333
    assert defaults["contextWindowTokens"] == 65_536
    assert "memoryWindow" not in defaults


def test_onboard_refresh_backfills_missing_channel_fields(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "channels": {
                    "qq": {
                        "enabled": False,
                        "appId": "",
                        "secret": "",
                        "allowFrom": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("ithqbot.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr(
        "ithqbot.channels.registry.discover_all",
        lambda: {
            "qq": SimpleNamespace(
                default_config=lambda: {
                    "enabled": False,
                    "appId": "",
                    "secret": "",
                    "allowFrom": [],
                    "msgFormat": "plain",
                }
            )
        },
    )

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["channels"]["qq"]["msgFormat"] == "plain"


def test_active_profiles_select_kafka_minio_and_redis_uri() -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"sessionStoreUri": None}},
            "infrastructure": {
                "kafka": {"servers": "default:9092"},
                "activeKafkaProfile": "prod",
                "kafkaProfiles": {"prod": {"servers": "prod:9092"}},
                "redis": {"uri": "redis://default:6379/0"},
                "activeRedisProfile": "prod",
                "redisProfiles": {"prod": {"uri": "redis://prod:6379/0"}},
            },
            "tools": {
                "minio": {"endpoint": "default:9000"},
                "activeMinioProfile": "prod",
                "minioProfiles": {"prod": {"endpoint": "prod:9000"}},
            },
        }
    )

    assert config.get_active_kafka_config().servers == "prod:9092"
    assert config.get_active_minio_config().endpoint == "prod:9000"
    assert config.get_active_session_store_uri() == "redis://prod:6379/0"


def test_session_store_uri_explicit_value_has_higher_priority_than_profiles() -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"sessionStoreUri": "redis://explicit:6379/0"}},
            "infrastructure": {
                "redis": {"uri": "redis://default:6379/0"},
                "activeRedisProfile": "prod",
                "redisProfiles": {"prod": {"uri": "redis://prod:6379/0"}},
            },
        }
    )
    assert config.get_active_session_store_uri() == "redis://explicit:6379/0"


def test_cron_store_uri_prefers_explicit_then_falls_back_to_memory_store() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "sessionStoreUri": "redis://session:6379/0",
                    "memoryStoreUri": "postgresql://memory:5432/ithqbot",
                    "cronStoreUri": "postgresql://cron:5432/ithqbot",
                }
            }
        }
    )
    assert config.get_active_cron_store_uri() == "postgresql://cron:5432/ithqbot"

    config_no_cron = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "sessionStoreUri": "redis://session:6379/0",
                    "memoryStoreUri": "postgresql://memory:5432/ithqbot",
                    "cronStoreUri": None,
                }
            }
        }
    )
    assert config_no_cron.get_active_cron_store_uri() == "postgresql://memory:5432/ithqbot"


def test_default_agent_store_settings_use_postgresql_for_memory_and_cron() -> None:
    config = Config()
    defaults = config.agents.defaults
    assert defaults.memory_store_uri == "postgresql://127.0.0.1:5432/ithqbot"
    assert defaults.cron_store_uri == "postgresql://127.0.0.1:5432/ithqbot"
    assert defaults.require_external_session_store is True
    assert defaults.require_external_memory_store is True
    assert defaults.require_external_cron_store is True


def test_create_session_store_supports_postgresql_plus_driver_scheme(tmp_path, monkeypatch) -> None:
    class _DummyStore:
        pass

    monkeypatch.setitem(_SESSION_STORE_FACTORIES, "postgresql", lambda _workspace, _uri: _DummyStore())
    store = create_session_store(
        workspace=tmp_path,
        config_uri="postgresql+psycopg://user:pass@localhost:5432/ithqbot",
        require_external_store=True,
    )
    assert isinstance(store, _DummyStore)


def test_create_session_store_requires_external_store_for_unknown_scheme(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_session_store(
            workspace=tmp_path,
            config_uri="mongodb://localhost:27017/ithqbot",
            require_external_store=True,
        )


def test_load_config_strict_mode_rejects_missing_active_provider_api_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "deepseek/deepseek-chat"}},
                "providers": {"deepseek": {"apiKey": "", "apiBase": "https://api.deepseek.com"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="deepseek"):
        load_config(config_path, strict_sensitive_config=True)


def test_load_config_warns_when_minio_credentials_are_incomplete(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "minio": {
                        "endpoint": "minio.internal:9000",
                        "accessKey": "ak",
                        "secretKey": "",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    load_config(config_path, strict_sensitive_config=False)
    output = capsys.readouterr().out

    assert "Object storage credentials are incomplete" in output


def test_create_session_store_fails_fast_after_init_retries(tmp_path: Path, monkeypatch) -> None:
    attempts = {"count": 0}

    class _BrokenStore:
        def __init__(self) -> None:
            self.client = self

        def ping(self) -> None:
            attempts["count"] += 1
            raise RuntimeError("redis unavailable")

    monkeypatch.setitem(_SESSION_STORE_FACTORIES, "redis", lambda _workspace, _uri: _BrokenStore())

    with pytest.raises(RuntimeError, match="Session store initialization failed"):
        create_session_store(
            workspace=tmp_path,
            config_uri="redis://localhost:6379/0",
            require_external_store=False,
        )

    assert attempts["count"] == 3


def test_create_session_store_rejects_missing_uri_without_local_fallback(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session_store_uri is not configured"):
        create_session_store(workspace=tmp_path, config_uri=None, require_external_store=False)
