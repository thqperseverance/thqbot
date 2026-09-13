import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from ithqbot.cli.commands import _execute_cron_job, _make_provider_for_model, app
from ithqbot.config.schema import Config
from ithqbot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule
from ithqbot.providers.openai_codex_provider import _strip_model_prefix
from ithqbot.providers.registry import find_by_model


def _strip_ansi(text):
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
    return ansi_escape.sub('', text)

runner = CliRunner()


class _StopGateway(RuntimeError):
    pass


@pytest.fixture
def mock_paths():
    with patch("ithqbot.config.loader.get_config_path") as mock_cp, \
         patch("ithqbot.config.loader.save_config") as mock_sc, \
         patch("ithqbot.config.loader.load_config") as mock_lc:

        base_dir = Path("./test_onboard_data")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir()

        config_file = base_dir / "config.json"

        mock_cp.return_value = config_file
        mock_sc.side_effect = lambda config: config_file.write_text("{}")

        yield config_file

        if base_dir.exists():
            shutil.rmtree(base_dir)


def test_onboard_fresh_install(mock_paths):
    config_file = mock_paths

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Created config" in result.stdout
    assert "已准备就绪" in result.stdout
    assert config_file.exists()


def test_onboard_existing_config_refresh(mock_paths):
    config_file = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "existing values preserved" in result.stdout


def test_onboard_existing_config_overwrite(mock_paths):
    config_file = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="y\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "Config reset to defaults" in result.stdout


def test_onboard_existing_workspace_safe_create(mock_paths):
    config_file = mock_paths
    config_file.write_text("{}")

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "已准备就绪" in result.stdout


def test_config_matches_github_copilot_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "github-copilot/gpt-5.3-codex"

    assert config.get_provider_name() == "github_copilot"


def test_config_matches_openai_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.1-codex"

    assert config.get_provider_name() == "openai_codex"


def test_config_matches_explicit_ollama_prefix_without_api_key():
    config = Config()
    config.agents.defaults.model = "ollama/llama3.2"

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_explicit_ollama_provider_uses_default_localhost_api_base():
    config = Config()
    config.agents.defaults.provider = "ollama"
    config.agents.defaults.model = "llama3.2"

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_auto_detects_ollama_from_local_api_base():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {"ollama": {"apiBase": "http://localhost:11434"}},
        }
    )

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_prefers_ollama_over_vllm_when_both_local_providers_configured():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {
                "vllm": {"apiBase": "http://localhost:8000"},
                "ollama": {"apiBase": "http://localhost:11434"},
            },
        }
    )

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_falls_back_to_vllm_when_ollama_not_configured():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {
                "vllm": {"apiBase": "http://localhost:8000"},
            },
        }
    )

    assert config.get_provider_name() == "vllm"
    assert config.get_api_base() == "http://localhost:8000"


def test_find_by_model_prefers_explicit_prefix_over_generic_codex_keyword():
    spec = find_by_model("github-copilot/gpt-5.3-codex")

    assert spec is not None
    assert spec.name == "github_copilot"

def test_openai_codex_strip_prefix_supports_hyphen_and_underscore():
    assert _strip_model_prefix("openai-codex/gpt-5.1-codex") == "gpt-5.1-codex"
    assert _strip_model_prefix("openai_codex/gpt-5.1-codex") == "gpt-5.1-codex"


def test_make_provider_for_model_strips_gateway_prefix_for_custom_provider():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "siliconflow/deepseek-ai/deepseek-v3"}},
            "providers": {
                "siliconflow": {
                    "apiKey": "test-key",
                    "apiBase": "https://api.siliconflow.cn/v1",
                }
            },
        }
    )
    captured = {}

    class FakeProvider:
        def __init__(self, api_key, api_base, default_model, extra_headers=None, model_api_bases=None):
            captured["api_key"] = api_key
            captured["api_base"] = api_base
            captured["default_model"] = default_model
            captured["extra_headers"] = extra_headers
            captured["model_api_bases"] = model_api_bases
            self.generation = None

    with patch("ithqbot.providers.custom_provider.CustomProvider", FakeProvider):
        _make_provider_for_model(config)

    assert captured["api_key"] == "test-key"
    assert captured["api_base"] == "https://api.siliconflow.cn/v1"
    assert captured["default_model"] == "deepseek-ai/deepseek-v3"


def test_make_provider_for_model_strips_local_provider_prefix():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "ollama/llama3.2"}},
            "providers": {"ollama": {"apiBase": "http://localhost:11434"}},
        }
    )
    captured = {}

    class FakeProvider:
        def __init__(self, api_key, api_base, default_model, extra_headers=None, model_api_bases=None):
            captured["api_key"] = api_key
            captured["api_base"] = api_base
            captured["default_model"] = default_model
            captured["extra_headers"] = extra_headers
            captured["model_api_bases"] = model_api_bases
            self.generation = None

    with patch("ithqbot.providers.custom_provider.CustomProvider", FakeProvider):
        _make_provider_for_model(config)

    assert captured["api_key"] == ""
    assert captured["api_base"] == "http://localhost:11434"
    assert captured["default_model"] == "llama3.2"


def test_make_provider_for_model_uses_model_specific_api_base_for_custom_provider():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "qwen2.5-72b-instruct"}},
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "http://10.0.0.1/default/v1",
                    "modelApiBases": {
                        "qwen2.5-72b-instruct": "http://10.0.0.1/qwen72b/v1",
                        "qwen2.5-32b-instruct": "http://10.0.0.1/qwen32b/v1",
                    },
                }
            },
        }
    )
    captured = {}

    class FakeProvider:
        def __init__(self, api_key, api_base, default_model, extra_headers=None, model_api_bases=None):
            captured["api_key"] = api_key
            captured["api_base"] = api_base
            captured["default_model"] = default_model
            captured["extra_headers"] = extra_headers
            captured["model_api_bases"] = model_api_bases
            self.generation = None

    with patch("ithqbot.providers.custom_provider.CustomProvider", FakeProvider):
        _make_provider_for_model(config)

    assert captured["api_key"] == "test-key"
    assert captured["api_base"] == "http://10.0.0.1/qwen72b/v1"
    assert captured["default_model"] == "qwen2.5-72b-instruct"


def test_make_provider_for_model_uses_model_specific_api_base_with_prefixed_model():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "custom/qwen2.5-32b-instruct"}},
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "http://10.0.0.1/default/v1",
                    "modelApiBases": {
                        "qwen2.5-32b-instruct": "http://10.0.0.1/qwen32b/v1",
                    },
                }
            },
        }
    )
    captured = {}

    class FakeProvider:
        def __init__(self, api_key, api_base, default_model, extra_headers=None, model_api_bases=None):
            captured["api_key"] = api_key
            captured["api_base"] = api_base
            captured["default_model"] = default_model
            captured["extra_headers"] = extra_headers
            captured["model_api_bases"] = model_api_bases
            self.generation = None

    with patch("ithqbot.providers.custom_provider.CustomProvider", FakeProvider):
        _make_provider_for_model(config)

    assert captured["api_base"] == "http://10.0.0.1/qwen32b/v1"
    assert captured["default_model"] == "qwen2.5-32b-instruct"


def test_make_provider_for_model_strips_alias_suffix_for_model_and_mapping():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "qwen72b@boss"}},
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "http://10.0.0.1/default/v1",
                    "modelApiBases": {
                        "qwen72b@boss": "http://10.0.0.1/qwen72b-boss/v1",
                    },
                }
            },
        }
    )
    captured = {}

    class FakeProvider:
        def __init__(self, api_key, api_base, default_model, extra_headers=None, model_api_bases=None):
            captured["api_key"] = api_key
            captured["api_base"] = api_base
            captured["default_model"] = default_model
            captured["extra_headers"] = extra_headers
            captured["model_api_bases"] = model_api_bases
            self.generation = None

    with patch("ithqbot.providers.custom_provider.CustomProvider", FakeProvider):
        _make_provider_for_model(config)

    assert captured["api_base"] == "http://10.0.0.1/qwen72b-boss/v1"
    assert captured["default_model"] == "qwen72b"


def test_make_provider_for_model_prefixed_alias_suffix_uses_stripped_fallback_mapping():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "custom/qwen72b@oa"}},
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "http://10.0.0.1/default/v1",
                    "modelApiBases": {
                        "qwen72b": "http://10.0.0.1/qwen72b-generic/v1",
                    },
                }
            },
        }
    )
    captured = {}

    class FakeProvider:
        def __init__(self, api_key, api_base, default_model, extra_headers=None, model_api_bases=None):
            captured["api_key"] = api_key
            captured["api_base"] = api_base
            captured["default_model"] = default_model
            captured["extra_headers"] = extra_headers
            captured["model_api_bases"] = model_api_bases
            self.generation = None

    with patch("ithqbot.providers.custom_provider.CustomProvider", FakeProvider):
        _make_provider_for_model(config)

    assert captured["api_base"] == "http://10.0.0.1/qwen72b-generic/v1"
    assert captured["default_model"] == "qwen72b"


def test_make_provider_for_model_prefers_custom_model_api_base_even_when_default_provider_differs():
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {"provider": "siliconflow", "model": "deepseek-ai/deepseek-v3"},
            },
            "providers": {
                "custom": {
                    "apiKey": "custom-key",
                    "apiBase": "http://fallback.invalid/v1",
                    "modelApiBases": {
                        "qwen3.5": "http://10.152.249.132:17009/llm/proxy/amcc/qwen3.5/v1",
                        "qwen3-32b": "http://10.152.249.132:17009/llm/proxy/amcc/qwen3-32b/v1",
                        "ds_r1": "http://10.152.249.132:17009/llm/proxy/ds_r1/v1",
                    },
                },
                "siliconflow": {"apiKey": "silicon-key", "apiBase": "https://api.siliconflow.cn/v1"},
            },
        }
    )
    captured = {}

    class FakeProvider:
        def __init__(self, api_key, api_base, default_model, extra_headers=None, model_api_bases=None):
            captured["api_key"] = api_key
            captured["api_base"] = api_base
            captured["default_model"] = default_model
            captured["extra_headers"] = extra_headers
            captured["model_api_bases"] = model_api_bases
            self.generation = None

    with patch("ithqbot.providers.custom_provider.CustomProvider", FakeProvider):
        _make_provider_for_model(config, "qwen3.5")

    assert captured["api_key"] == "custom-key"
    assert captured["api_base"] == "http://10.152.249.132:17009/llm/proxy/amcc/qwen3.5/v1"
    assert captured["default_model"] == "qwen3.5"
    assert captured["model_api_bases"]["ds_r1"] == "http://10.152.249.132:17009/llm/proxy/ds_r1/v1"


@pytest.fixture
def mock_agent_runtime(tmp_path):
    """Mock agent command dependencies for focused CLI tests."""
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "default-workspace")
    cron_dir = tmp_path / "data" / "cron"

    with patch("ithqbot.config.loader.load_config", return_value=config) as mock_load_config, \
         patch("ithqbot.config.paths.get_cron_dir", return_value=cron_dir), \
         patch("ithqbot.cli.commands._make_provider", return_value=object()), \
         patch("ithqbot.cli.commands._print_agent_response") as mock_print_response, \
         patch("ithqbot.bus.queue.MessageBus"), \
         patch("ithqbot.cron.service.CronService"), \
         patch("ithqbot.agent.loop.AgentLoop") as mock_agent_loop_cls:

        agent_loop = MagicMock()
        agent_loop.channels_config = None
        agent_loop.process_direct = AsyncMock(return_value="mock-response")
        agent_loop.close_mcp = AsyncMock(return_value=None)
        mock_agent_loop_cls.return_value = agent_loop

        yield {
            "config": config,
            "load_config": mock_load_config,
            "agent_loop_cls": mock_agent_loop_cls,
            "agent_loop": agent_loop,
            "print_response": mock_print_response,
        }


def test_agent_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["agent", "--help"])

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    assert "--workspace" in stripped_output
    assert "-w" in stripped_output
    assert "--config" in stripped_output
    assert "-c" in stripped_output


def test_agent_uses_default_config_when_no_workspace_or_config_flags(mock_agent_runtime):
    result = runner.invoke(app, ["agent", "-m", "hello"])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (None,)
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == (
        mock_agent_runtime["config"].workspace_path
    )
    mock_agent_runtime["agent_loop"].process_direct.assert_awaited_once()
    mock_agent_runtime["print_response"].assert_called_once_with("mock-response", render_markdown=True)


def test_agent_uses_explicit_config_path(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)


def test_agent_config_sets_active_path(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "ithqbot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr("ithqbot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("ithqbot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("ithqbot.cron.service.CronService", lambda _store, **_kwargs: object())

    class _FakeAgentLoop:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs) -> str:
            return "ok"

        async def close_mcp(self) -> None:
            return None

    monkeypatch.setattr("ithqbot.agent.loop.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("ithqbot.cli.commands._print_agent_response", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert seen["config_path"] == config_file.resolve()


def test_agent_overrides_workspace_path(mock_agent_runtime):
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(app, ["agent", "-m", "hello", "-w", str(workspace_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == workspace_path


def test_agent_workspace_override_wins_over_config_workspace(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(
        app,
        ["agent", "-m", "hello", "-c", str(config_path), "-w", str(workspace_path)],
    )

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == workspace_path


def test_agent_warns_about_deprecated_memory_window(mock_agent_runtime):
    mock_agent_runtime["config"].agents.defaults.memory_window = 100

    result = runner.invoke(app, ["agent", "-m", "hello"])

    assert result.exit_code == 0
    assert "memoryWindow" in result.stdout
    assert "contextWindowTokens" in result.stdout


def test_gateway_uses_workspace_from_config_by_default(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "ithqbot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr(
        "ithqbot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGateway)
    assert seen["config_path"] == config_file.resolve()
    assert config.workspace_path == Path(config.agents.defaults.workspace)


def test_gateway_workspace_option_overrides_config(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    override = tmp_path / "override-workspace"

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr(
        "ithqbot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(
        app,
        ["gateway", "--config", str(config_file), "--workspace", str(override)],
    )

    assert isinstance(result.exception, _StopGateway)
    assert config.workspace_path == override


def test_gateway_warns_about_deprecated_memory_window(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.memory_window = 100

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr(
        "ithqbot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGateway)
    assert "memoryWindow" in result.stdout
    assert "contextWindowTokens" in result.stdout

def test_gateway_uses_external_cron_store_without_local_jobs_file(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    config.agents.defaults.session_store_uri = f"file://{(tmp_path / 'sessions').as_posix()}"
    config.agents.defaults.require_external_session_store = False
    seen: dict[str, object] = {}

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr("ithqbot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("ithqbot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("ithqbot.session.manager.SessionManager", lambda *_args, **_kwargs: object())

    class _StopCron:
        def __init__(self, store_path: Path | None, **_kwargs) -> None:
            seen["cron_store"] = store_path
            raise _StopGateway("stop")

    monkeypatch.setattr("ithqbot.cron.service.CronService", _StopCron)

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGateway)
    assert seen["cron_store"] is None


def test_gateway_uses_configured_port_when_cli_flag_is_missing(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.gateway.port = 18791

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr(
        "ithqbot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGateway)
    assert "port 18791" in result.stdout


def test_gateway_cli_port_overrides_configured_port(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.gateway.port = 18791

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr(
        "ithqbot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file), "--port", "18792"])

    assert isinstance(result.exception, _StopGateway)
    assert "port 18792" in result.stdout


def test_runtime_uses_workspace_from_config_by_default(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "ithqbot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr(
        "ithqbot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(app, ["runtime", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGateway)
    assert seen["config_path"] == config_file.resolve()
    assert config.workspace_path == Path(config.agents.defaults.workspace)
    assert "Starting ithqbot runtime" in result.stdout


def test_gateway_alias_prints_runtime_deprecation_note(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr(
        "ithqbot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGateway)
    assert "Starting ithqbot runtime" in result.stdout
    assert "compatibility alias" in result.stdout
    assert "prefer `runtime`" in result.stdout


def test_runtime_without_cron_skips_cron_service(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.session_store_uri = f"file://{(tmp_path / 'sessions').as_posix()}"
    config.agents.defaults.require_external_session_store = False
    seen: dict[str, object] = {}

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr("ithqbot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("ithqbot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("ithqbot.session.manager.SessionManager", lambda *_args, **_kwargs: object())

    def _stop_agent_loop(*_args, **kwargs):
        seen["cron_service"] = kwargs["cron_service"]
        raise _StopGateway("stop")

    monkeypatch.setattr("ithqbot.agent.loop.AgentLoop", _stop_agent_loop)
    monkeypatch.setattr(
        "ithqbot.cron.service.CronService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("CronService should not be created")),
    )

    result = runner.invoke(app, ["runtime", "--config", str(config_file), "--without-cron"])

    assert isinstance(result.exception, _StopGateway)
    assert seen["cron_service"] is None


def test_runtime_without_channels_skips_channel_manager(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.session_store_uri = f"file://{(tmp_path / 'sessions').as_posix()}"
    config.agents.defaults.require_external_session_store = False

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr("ithqbot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("ithqbot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("ithqbot.session.manager.SessionManager", lambda *_args, **_kwargs: object())

    class _FakeCron:
        external_store = object()

        def list_jobs(self, _include_disabled=True):
            return []

        def add_job(self, **_kwargs):
            return None

        def status(self):
            return {"jobs": 0}

    class _FakeAgent:
        model = "test-model"

    monkeypatch.setattr("ithqbot.cron.service.CronService", lambda *_args, **_kwargs: _FakeCron())
    monkeypatch.setattr("ithqbot.agent.loop.AgentLoop", lambda *_args, **_kwargs: _FakeAgent())
    monkeypatch.setattr(
        "ithqbot.channels.manager.ChannelManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ChannelManager should not be created")),
    )
    monkeypatch.setattr(
        "ithqbot.heartbeat.service.HeartbeatService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_StopGateway("stop")),
    )

    result = runner.invoke(app, ["runtime", "--config", str(config_file), "--without-channels"])

    assert isinstance(result.exception, _StopGateway)


def test_runtime_without_heartbeat_skips_heartbeat_service(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.session_store_uri = f"file://{(tmp_path / 'sessions').as_posix()}"
    config.agents.defaults.require_external_session_store = False

    monkeypatch.setattr("ithqbot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("ithqbot.config.loader.load_config", lambda _path=None, **_kwargs: config)
    monkeypatch.setattr("ithqbot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("ithqbot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("ithqbot.session.manager.SessionManager", lambda *_args, **_kwargs: object())

    class _FakeCron:
        external_store = object()

        def list_jobs(self, _include_disabled=True):
            return []

        def add_job(self, **_kwargs):
            return None

        def status(self):
            return {"jobs": 0}

    class _FakeAgent:
        model = "test-model"

    class _FakeChannels:
        enabled_channels: list[str] = []

    def _stop_asyncio_run(coro):
        coro.close()
        raise _StopGateway("stop")

    monkeypatch.setattr("ithqbot.cron.service.CronService", lambda *_args, **_kwargs: _FakeCron())
    monkeypatch.setattr("ithqbot.agent.loop.AgentLoop", lambda *_args, **_kwargs: _FakeAgent())
    monkeypatch.setattr("ithqbot.channels.manager.ChannelManager", lambda *_args, **_kwargs: _FakeChannels())
    monkeypatch.setattr(
        "ithqbot.heartbeat.service.HeartbeatService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("HeartbeatService should not be created")),
    )
    monkeypatch.setattr("ithqbot.cli.commands.asyncio.run", _stop_asyncio_run)

    result = runner.invoke(app, ["runtime", "--config", str(config_file), "--without-heartbeat"])

    assert isinstance(result.exception, _StopGateway)


@pytest.mark.asyncio
async def test_execute_cron_job_publishes_outbound_with_duck_typed_message_tool(monkeypatch) -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()

    fake_cron_tool = MagicMock()
    cron_token = object()
    fake_cron_tool.set_cron_context.return_value = cron_token
    fake_message_tool = SimpleNamespace(_sent_in_turn=False)

    class _Tools:
        def get(self, name: str):
            return {"cron": fake_cron_tool, "message": fake_message_tool}.get(name)

    agent = MagicMock()
    agent.tools = _Tools()
    agent.model = "test-model"
    agent.process_direct = AsyncMock(return_value="去喝茶")

    monkeypatch.setattr("ithqbot.utils.evaluator.evaluate_response", AsyncMock(return_value=True))

    job = CronJob(
        id="cron-1",
        name="喝茶提醒",
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(
            message="提醒我喝茶",
            deliver=True,
            channel="icatmsg",
            to="u1",
            metadata={"tenant_id": "t1"},
        ),
        state=CronJobState(),
    )

    result = await _execute_cron_job(agent=agent, bus=bus, provider=object(), job=job)

    assert result == "去喝茶"
    bus.publish_outbound.assert_awaited_once()
    fake_cron_tool.set_cron_context.assert_called_once_with(True)
    fake_cron_tool.reset_cron_context.assert_called_once_with(cron_token)


@pytest.mark.asyncio
async def test_execute_cron_job_skips_fallback_publish_when_message_tool_already_sent(monkeypatch) -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()

    class _Tools:
        def get(self, name: str):
            if name == "message":
                return SimpleNamespace(_sent_in_turn=True)
            return None

    agent = MagicMock()
    agent.tools = _Tools()
    agent.model = "test-model"
    agent.process_direct = AsyncMock(return_value="已发送提醒")

    eval_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("ithqbot.utils.evaluator.evaluate_response", eval_mock)

    job = CronJob(
        id="cron-2",
        name="喝茶提醒",
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(
            message="提醒我喝茶",
            deliver=True,
            channel="icatmsg",
            to="u1",
            metadata={},
        ),
        state=CronJobState(),
    )

    result = await _execute_cron_job(agent=agent, bus=bus, provider=object(), job=job)

    assert result == "已发送提醒"
    bus.publish_outbound.assert_not_awaited()
    eval_mock.assert_not_awaited()
