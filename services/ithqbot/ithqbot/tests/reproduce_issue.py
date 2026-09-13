"""Tests for BootstrapConfig extra-fields handling.

Verifies that BootstrapConfig silently ignores fields belonging to the
full Config (bot, agents, providers, etc.) when the entire config.json
is passed during bootstrap.
"""

import pytest
from ithqbot.config.schema import BootstrapConfig, ConfigStoreType


def test_bootstrap_ignores_full_config_fields():
    """BootstrapConfig should ignore extra top-level keys from the full config."""
    full_config = {
        "bot": {"id": "bot_A"},
        "agents": {"defaults": {"model": "gpt-4"}},
        "providers": {"openai": {"apiKey": "sk-xxx"}},
        "config_store_type": "file",
    }
    bootstrap = BootstrapConfig(**full_config)
    assert bootstrap.config_store_type == ConfigStoreType.FILE
    assert bootstrap.bot_id == "default"


def test_bootstrap_with_only_bootstrap_fields():
    """BootstrapConfig works when given only its own fields."""
    data = {
        "config_store_type": "redis",
        "config_store_uri": "redis://localhost:6379/0",
        "bot_id": "bot_X",
        "encryption_enabled": True,
        "encryption_algorithm": "sm4",
        "encryption_key": "secret",
    }
    bootstrap = BootstrapConfig(**data)
    assert bootstrap.config_store_type == ConfigStoreType.REDIS
    assert bootstrap.config_store_uri == "redis://localhost:6379/0"
    assert bootstrap.bot_id == "bot_X"
    assert bootstrap.encryption_enabled is True
    assert bootstrap.encryption_algorithm == "sm4"
    assert bootstrap.encryption_key == "secret"


def test_bootstrap_with_empty_dict():
    """BootstrapConfig should use defaults when given an empty dict."""
    bootstrap = BootstrapConfig()
    assert bootstrap.config_store_type == ConfigStoreType.FILE
    assert bootstrap.config_store_uri is None
    assert bootstrap.bot_id == "default"
    assert bootstrap.encryption_enabled is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
