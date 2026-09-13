"""CLI commands for ithqbot."""

import asyncio
from contextlib import contextmanager, nullcontext
import os
import select
import signal
import sys
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from prompt_toolkit import print_formatted_text
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.application import run_in_terminal
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ithqbot import __logo__, __version__
from ithqbot.config.schema import Config
from ithqbot.observability import (
    configure_observability,
    get_account_llm_stats,
    get_recent_audit_logs,
)

app = typer.Typer(
    name="ithqbot",
    help=f"{__logo__} ithqbot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _cron_message_sent_in_turn(message_tool: Any) -> bool:
    """Check whether the message tool already sent a reply for the current cron turn."""
    return bool(getattr(message_tool, "_sent_in_turn", False))


async def _deliver_cron_job_response(
    *,
    agent: Any,
    bus: Any,
    provider: Any,
    job: Any,
    response: str | None,
) -> str | None:
    message_tool = agent.tools.get("message")
    if _cron_message_sent_in_turn(message_tool):
        return response

    if not (job.payload.deliver and job.payload.to and response):
        return response

    from ithqbot.bus.events import OutboundMessage
    from ithqbot.utils.evaluator import evaluate_response

    should_notify = await evaluate_response(
        response,
        job.payload.message,
        provider,
        agent.model,
    )
    if not should_notify:
        return response

    await bus.publish_outbound(
        OutboundMessage(
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to,
            content=response,
            metadata=job.payload.metadata,
        )
    )
    return response


async def _execute_cron_job(
    *,
    agent: Any,
    bus: Any,
    provider: Any,
    job: Any,
) -> str | None:
    """Execute a scheduled cron job through the agent loop."""
    from ithqbot.config.paths import get_workspace_path
    from ithqbot.utils.cleanup import cleanup_old_files

    if job.payload.metadata.get("system_task") == "cleanup":
        sessions_dir = get_workspace_path() / "sessions"
        cleanup_old_files(sessions_dir, max_age_hours=24)
        return "System cleanup completed."

    reminder_note = (
        "[Scheduled Task] Timer finished.\n\n"
        f"Task '{job.name}' has been triggered.\n"
        f"Scheduled instruction: {job.payload.message}"
    )

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if cron_tool is not None and hasattr(cron_tool, "set_cron_context"):
        cron_token = cron_tool.set_cron_context(True)
    try:
        response = await agent.process_direct(
            reminder_note,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            metadata=job.payload.metadata,
        )
    finally:
        if cron_tool is not None and cron_token is not None and hasattr(cron_tool, "reset_cron_context"):
            cron_tool.reset_cron_context(cron_token)

    return await _deliver_cron_job_response(
        agent=agent,
        bus=bus,
        provider=provider,
        job=job,
        response=response,
    )


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from ithqbot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} ithqbot[/cyan]")
    console.print(body)
    console.print()


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(response: str, render_markdown: bool) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} ithqbot[/cyan]"),
                c.print(Markdown(content) if render_markdown else Text(content)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


class _ThinkingSpinner:
    """Spinner wrapper with pause support for clean progress output."""

    def __init__(self, enabled: bool):
        self._spinner = console.status(
            "[dim]ithqbot is thinking...[/dim]", spinner="dots"
        ) if enabled else None
        self._active = False

    def __enter__(self):
        if self._spinner:
            self._spinner.start()
        self._active = True
        return self

    def __exit__(self, *exc):
        self._active = False
        if self._spinner:
            self._spinner.stop()
        return False

    @contextmanager
    def pause(self):
        """Temporarily stop spinner while printing progress."""
        if self._spinner and self._active:
            self._spinner.stop()
        try:
            yield
        finally:
            if self._spinner and self._active:
                self._spinner.start()


def _print_cli_progress_line(text: str, thinking: _ThinkingSpinner | None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


async def _print_interactive_progress_line(text: str, thinking: _ThinkingSpinner | None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} ithqbot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """ithqbot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize ithqbot configuration."""
    from ithqbot.config.loader import get_config_path, load_config, save_config
    from ithqbot.config.schema import Config

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
        if typer.confirm("Overwrite?"):
            config = Config()
            save_config(config)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = load_config()
            save_config(config)
            console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        save_config(Config())
        console.print(f"[green]✓[/green] Created config at {config_path}")

    console.print("[dim]配置模板现已使用 `maxTokens` + `contextWindowTokens`；`memoryWindow` 不再作为运行时配置项。[/dim]")

    _onboard_plugins(config_path)

    console.print(f"\n{__logo__} ithqbot 已准备就绪！")
    console.print("\n下一步：")
    console.print("  1. 在 [cyan]~/.ithqbot/config.json[/cyan] 中填入 API Key")
    console.print("     可在 https://openrouter.ai/keys 获取")
    console.print("  2. 开始对话：[cyan]ithqbot agent -m \"你好！\"[/cyan]")
    console.print("\n[dim]需要 Telegram / WhatsApp 配置时，请查看 README 的“聊天应用”章节。[/dim]")


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from ithqbot.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_provider_for_model(config: Config, model: str | None = None):
    """Create the appropriate LLM provider from config for a specific model."""
    from ithqbot.providers.base import GenerationSettings
    from ithqbot.providers.openai_codex_provider import OpenAICodexProvider
    from ithqbot.providers.azure_openai_provider import AzureOpenAIProvider
    from ithqbot.providers.custom_provider import CustomProvider
    from ithqbot.providers.registry import find_by_name, find_gateway

    if model is None:
        model = config.agents.defaults.model

    def _strip_model_alias_suffix(value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return raw
        head, sep, tail = raw.rpartition("@")
        if not sep:
            return raw
        if "/" in tail:
            return raw
        return head or raw

    model_for_provider = _strip_model_alias_suffix(model)

    def _custom_model_api_bases() -> dict[str, str]:
        provider_cfg = getattr(config.providers, "custom", None)
        raw_mappings = getattr(provider_cfg, "model_api_bases", None)
        merged: dict[str, str] = {}
        if isinstance(raw_mappings, dict):
            for raw_model, raw_base in raw_mappings.items():
                model_name = str(raw_model or "").strip()
                api_base = str(raw_base or "").strip()
                if model_name and api_base:
                    merged[model_name] = api_base
        return merged

    def _resolve_custom_model_api_base(*candidates: str | None) -> str | None:
        mappings = _custom_model_api_bases()
        if not mappings:
            return None
        for candidate in candidates:
            key = str(candidate or "").strip()
            if not key:
                continue
            direct = mappings.get(key)
            if isinstance(direct, str) and direct.strip():
                return direct
        normalized = {
            str(k).strip().lower(): str(v).strip()
            for k, v in mappings.items()
            if str(k).strip() and str(v).strip()
        }
        for candidate in candidates:
            key = str(candidate or "").strip().lower()
            if not key:
                continue
            direct = normalized.get(key)
            if isinstance(direct, str) and direct:
                return direct
        return None

    provider_name = config.get_provider_name(model_for_provider)
    p = config.get_provider(model_for_provider)

    def _merged_model_api_bases(provider_cfg) -> dict[str, str]:
        merged: dict[str, str] = {}
        raw_mappings = getattr(provider_cfg, "model_api_bases", None)
        if isinstance(raw_mappings, dict):
            for raw_model, raw_base in raw_mappings.items():
                model_name = str(raw_model or "").strip()
                api_base = str(raw_base or "").strip()
                if model_name and api_base:
                    merged[model_name] = api_base
        return merged

    def _resolve_direct_model_name() -> str:
        resolved = model_for_provider or config.agents.defaults.model
        spec = find_gateway(
            provider_name=provider_name,
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model_for_provider),
        ) or (find_by_name(provider_name) if provider_name else None)
        if provider_name == "openai" and resolved.startswith("openai/"):
            return resolved.split("/", 1)[1]
        if "/" in resolved:
            prefix, remainder = resolved.split("/", 1)
            normalized_prefix = prefix.lower().replace("-", "_")
            provider_aliases = {str(provider_name or "").lower().replace("-", "_")}
            if spec and spec.model_prefix:
                provider_aliases.add(spec.model_prefix.lower().replace("-", "_"))
            if normalized_prefix in provider_aliases:
                return remainder
            if spec and spec.strip_model_prefix:
                return remainder
        return resolved

    def _resolve_api_base_for_model(
        provider_cfg,
        *,
        raw_model_name: str,
        normalized_model_name: str,
        direct_model_name_raw: str,
        direct_model_name: str,
    ) -> str | None:
        mappings = _merged_model_api_bases(provider_cfg)
        if mappings:
            for candidate in (
                raw_model_name,
                normalized_model_name,
                direct_model_name_raw,
                direct_model_name,
            ):
                base = mappings.get(candidate)
                if isinstance(base, str) and base.strip():
                    return base
            normalized = {
                str(k).strip().lower(): str(v).strip()
                for k, v in mappings.items()
                if str(k).strip() and str(v).strip()
            }
            for candidate in (
                raw_model_name,
                normalized_model_name,
                direct_model_name_raw,
                direct_model_name,
            ):
                base = normalized.get(str(candidate).strip().lower())
                if isinstance(base, str) and base:
                    return base
        return config.get_api_base(normalized_model_name)

    direct_model_name_raw = _resolve_direct_model_name()
    direct_model_name = _strip_model_alias_suffix(direct_model_name_raw)
    resolved_api_base = _resolve_api_base_for_model(
        p,
        raw_model_name=model,
        normalized_model_name=model_for_provider,
        direct_model_name_raw=direct_model_name_raw,
        direct_model_name=direct_model_name,
    )
    custom_api_base = _resolve_custom_model_api_base(
        model,
        model_for_provider,
        direct_model_name_raw,
        direct_model_name,
    )
    if custom_api_base:
        provider_name = "custom"
        p = getattr(config.providers, "custom", None) or p
        resolved_api_base = custom_api_base

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model_for_provider.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    elif provider_name == "custom":
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=resolved_api_base or "http://localhost:8000/v1",
            default_model=direct_model_name,
            extra_headers=p.extra_headers if p else None,
            model_api_bases=_merged_model_api_bases(p),
        )
    # Azure OpenAI: direct Azure OpenAI endpoint with deployment name
    elif provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]错误：Azure OpenAI 需要同时配置 api_key 和 api_base。[/red]")
            console.print("请在 ~/.ithqbot/config.json 的 providers.azure_openai 段落中设置。")
            console.print("模型部署名请填写在 model 字段中。")
            raise typer.Exit(1)
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    else:
        spec = find_by_name(provider_name)
        if not model_for_provider.startswith("bedrock/") and not (p and p.api_key) and not (spec and (spec.is_oauth or spec.is_local)):
            console.print(f"[red]错误：提供方“{provider_name}”未配置 API Key。[/red]")
            console.print("请在 ~/.ithqbot/config.json 的 providers 段落中补充配置。")
            raise typer.Exit(1)
        api_base = resolved_api_base
        if not api_base and provider_name not in {"openai"}:
            console.print(f"[red]错误：提供方“{provider_name}”缺少兼容 OpenAI 的 api_base，当前不可用。[/red]")
            raise typer.Exit(1)
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=api_base or "https://api.openai.com/v1",
            default_model=direct_model_name,
            extra_headers=p.extra_headers if p else None,
            model_api_bases=_merged_model_api_bases(p),
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def _make_provider(config: Config):
    """Create the default LLM provider."""
    return _make_provider_for_model(config)


def _make_provider_factory(config: Config):
    """Create a factory that can produce LLM providers for any model."""
    return lambda model_name: _make_provider_for_model(config, model_name)


def _load_runtime_config(
    config: str | None = None, workspace: str | None = None, bot_id: str | None = None
) -> Config:
    """Load config and optionally override the active workspace."""
    from ithqbot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path, bot_id=bot_id, strict_sensitive_config=True)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _resolve_observability_redis_uri(config: Config) -> str | None:
    env_uri = os.getenv("ITHQBOT_OBSERVABILITY_REDIS_URI")
    if env_uri:
        return env_uri
    uri = config.get_active_session_store_uri()
    if uri and uri.startswith(("redis://", "rediss://")):
        return uri
    return None


def _print_deprecated_memory_window_notice(config: Config) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        console.print(
            "[yellow]Hint:[/yellow] Detected deprecated `memoryWindow` without "
            "`contextWindowTokens`. `memoryWindow` is ignored; run "
            "[cyan]ithqbot onboard[/cyan] to refresh your config template."
        )


# ============================================================================
# Runtime Worker
# ============================================================================


def _run_runtime_command(
    *,
    port: int | None,
    health_port: int | None,
    workspace: str | None,
    verbose: bool,
    config: str | None,
    bot_id: str | None,
    channels_enabled: bool | None,
    cron_enabled: bool | None,
    heartbeat_enabled: bool | None,
    invoked_as_gateway: bool,
) -> None:
    from loguru import logger

    from ithqbot.agent.loop import AgentLoop
    from ithqbot.agent.runtime import ExecutorConfig
    from ithqbot.api.health import start_health_server
    from ithqbot.bus.queue import MessageBus
    from ithqbot.channels.manager import ChannelManager
    from ithqbot.cron.service import CronService
    from ithqbot.cron.types import CronJob
    from ithqbot.heartbeat.service import HeartbeatService
    from ithqbot.session.factory import create_session_store
    from ithqbot.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace, bot_id=bot_id)
    _print_deprecated_memory_window_notice(config)
    port = port if port is not None else config.gateway.port

    enable_channels = True if channels_enabled is None else channels_enabled
    enable_cron = True if cron_enabled is None else cron_enabled
    hb_cfg = config.gateway.heartbeat
    enable_heartbeat = hb_cfg.enabled if heartbeat_enabled is None else heartbeat_enabled

    console.print(f"{__logo__} Starting ithqbot runtime version {__version__} on port {port}...")
    if invoked_as_gateway:
        console.print("[yellow]Note:[/yellow] `gateway` is a compatibility alias; prefer `runtime`.")

    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(
        config.workspace_path,
        store=create_session_store(
            config.workspace_path,
            config_uri=config.get_active_session_store_uri(),
            require_external_store=config.agents.defaults.require_external_session_store,
        ),
    )

    cron: CronService | None = None
    if enable_cron:
        cron = CronService(
            None,
            store_uri=config.get_active_cron_store_uri(),
            require_external_store=config.agents.defaults.require_external_cron_store,
        )

        try:
            existing_jobs = cron.list_jobs(include_disabled=True)
            if not any(j.name == "ithqbot 系统清理" for j in existing_jobs):
                from ithqbot.cron.types import CronSchedule

                cron.add_job(
                    name="ithqbot 系统清理",
                    schedule=CronSchedule(kind="every", every_ms=3600 * 1000),
                    message="执行系统清理：删除超过24小时的临时文件",
                    metadata={"system_task": "cleanup"},
                )
        except Exception as e:
            logger.warning("注入默认清理任务失败: {}", e)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        executor_config=ExecutorConfig(max_concurrency=config.agents.defaults.max_tool_concurrency),
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        minio_config=config.get_active_storage_config(),
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        bot_config=config.bot.model_dump() if config.bot else None,
        routing_config=config.agents.routing,
        bot_guardrails_config=config.bot_guardrails,
        tools_config=config.tools,
        skills_config=config.skills,
        memory_store_uri=config.get_active_memory_store_uri(),
        session_store_uri=config.get_active_session_store_uri(),
        require_external_memory_store=config.agents.defaults.require_external_memory_store,
        require_external_session_store=config.agents.defaults.require_external_session_store,
        config=config,
        provider_factory=_make_provider_factory(config),
    )

    if cron is not None:

        async def on_cron_job(job: CronJob) -> str | None:
            """Execute a cron job through the agent."""
            return await _execute_cron_job(
                agent=agent,
                bus=bus,
                provider=provider,
                job=job,
            )

        cron.on_job = on_cron_job

    channels: ChannelManager | None = ChannelManager(config, bus) if enable_channels else None

    def _enabled_channel_names() -> list[str]:
        if channels is None:
            return []
        return list(channels.enabled_channels)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(_enabled_channel_names())
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        return "cli", "direct"

    heartbeat: HeartbeatService | None = None
    if enable_heartbeat:

        async def on_heartbeat_execute(tasks: str) -> str:
            """Execute heartbeat tasks through the full agent loop."""
            channel, chat_id = _pick_heartbeat_target()

            async def _silent(*_args, **_kwargs):
                pass

            return await agent.process_direct(
                tasks,
                session_key="heartbeat",
                channel=channel,
                chat_id=chat_id,
                on_progress=_silent,
            )

        async def on_heartbeat_notify(response: str) -> None:
            """Deliver a heartbeat response to the user's channel."""
            from ithqbot.bus.events import OutboundMessage

            channel, chat_id = _pick_heartbeat_target()
            if channel == "cli":
                return
            await bus.publish_outbound(
                OutboundMessage(channel=channel, chat_id=chat_id, content=response)
            )

        heartbeat_lease_name = (
            f"heartbeat:{config.bot.id}" if hb_cfg.lease_enabled and config.bot.id else None
        )
        heartbeat = HeartbeatService(
            workspace=config.workspace_path,
            provider=provider,
            model=agent.model,
            on_execute=on_heartbeat_execute,
            on_notify=on_heartbeat_notify,
            interval_s=hb_cfg.interval_s,
            enabled=True,
            lease_store=cron.external_store if hb_cfg.lease_enabled and cron is not None else None,
            lease_name=heartbeat_lease_name,
            lease_ttl_s=hb_cfg.lease_ttl_s,
        )

    enabled_channels = _enabled_channel_names()
    if channels is None:
        console.print("[dim]Channels: disabled by CLI[/dim]")
    elif enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    if cron is None:
        console.print("[dim]Cron: disabled by CLI[/dim]")
    else:
        cron_status = cron.status()
        if cron_status["jobs"] > 0:
            console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    if heartbeat is None:
        console.print("[dim]Heartbeat: disabled[/dim]")
    else:
        console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            configure_observability(_resolve_observability_redis_uri(config), config.observability)
            if cron is not None:
                await cron.start()
            if heartbeat is not None:
                await heartbeat.start()

            h_port = health_port if health_port is not None else getattr(config.gateway, "health_port", 9002)
            tasks = [agent.run(), start_health_server(h_port, channels)]
            if channels is not None:
                tasks.append(channels.start_all())
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Runtime crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            if heartbeat is not None:
                heartbeat.stop()
            if cron is not None:
                cron.stop()
            agent.stop()
            if channels is not None:
                await channels.stop_all()

    asyncio.run(run())


@app.command("runtime")
def runtime(
    port: int | None = typer.Option(None, "--port", "-p", help="Runtime port"),
    health_port: int | None = typer.Option(None, "--health-port", help="Health check port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    bot_id: str | None = typer.Option(None, "--bot-id", "-b", help="Bot ID to load"),
    channels_enabled: bool | None = typer.Option(
        None, "--with-channels/--without-channels", help="Enable external channel adapters"
    ),
    cron_enabled: bool | None = typer.Option(
        None, "--with-cron/--without-cron", help="Enable cron scheduling"
    ),
    heartbeat_enabled: bool | None = typer.Option(
        None, "--with-heartbeat/--without-heartbeat", help="Enable heartbeat execution"
    ),
):
    """Start the ithqbot runtime worker."""
    _run_runtime_command(
        port=port,
        health_port=health_port,
        workspace=workspace,
        verbose=verbose,
        config=config,
        bot_id=bot_id,
        channels_enabled=channels_enabled,
        cron_enabled=cron_enabled,
        heartbeat_enabled=heartbeat_enabled,
        invoked_as_gateway=False,
    )


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Runtime port"),
    health_port: int | None = typer.Option(None, "--health-port", help="Health check port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    bot_id: str | None = typer.Option(None, "--bot-id", "-b", help="Bot ID to load"),
    channels_enabled: bool | None = typer.Option(
        None, "--with-channels/--without-channels", help="Enable external channel adapters"
    ),
    cron_enabled: bool | None = typer.Option(
        None, "--with-cron/--without-cron", help="Enable cron scheduling"
    ),
    heartbeat_enabled: bool | None = typer.Option(
        None, "--with-heartbeat/--without-heartbeat", help="Enable heartbeat execution"
    ),
):
    """Start the ithqbot runtime worker via the deprecated gateway alias."""
    _run_runtime_command(
        port=port,
        health_port=health_port,
        workspace=workspace,
        verbose=verbose,
        config=config,
        bot_id=bot_id,
        channels_enabled=channels_enabled,
        cron_enabled=cron_enabled,
        heartbeat_enabled=heartbeat_enabled,
        invoked_as_gateway=True,
    )




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    bot_id: str | None = typer.Option(None, "--bot-id", "-b", help="Bot ID to load"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show ithqbot runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from ithqbot.agent.loop import AgentLoop
    from ithqbot.agent.runtime import ExecutorConfig
    from ithqbot.bus.queue import MessageBus
    from ithqbot.cron.service import CronService

    config = _load_runtime_config(config, workspace, bot_id=bot_id)
    configure_observability(_resolve_observability_redis_uri(config), config.observability)
    _print_deprecated_memory_window_notice(config)
    bus = MessageBus()
    provider = _make_provider(config)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron = CronService(
        None,
        store_uri=config.get_active_cron_store_uri(),
        require_external_store=config.agents.defaults.require_external_cron_store,
    )

    if logs:
        logger.enable("ithqbot")
    else:
        logger.disable("ithqbot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        executor_config=ExecutorConfig(max_concurrency=config.agents.defaults.max_tool_concurrency),
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        minio_config=config.get_active_storage_config(),
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        bot_config=config.bot.model_dump() if config.bot else None,
        routing_config=config.agents.routing,
        bot_guardrails_config=config.bot_guardrails,
        tools_config=config.tools,
        skills_config=config.skills,
        memory_store_uri=config.get_active_memory_store_uri(),
        session_store_uri=config.get_active_session_store_uri(),
        require_external_memory_store=config.agents.defaults.require_external_memory_store,
        require_external_session_store=config.agents.defaults.require_external_session_store,
        config=config,
        provider_factory=_make_provider_factory(config),
    )

    # Shared reference for progress callbacks
    _thinking: _ThinkingSpinner | None = None

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        _print_cli_progress_line(content, _thinking)

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            nonlocal _thinking
            configure_observability(_resolve_observability_redis_uri(config), config.observability)
            _thinking = _ThinkingSpinner(enabled=not logs)
            with _thinking:
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            _thinking = None
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from ithqbot.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            configure_observability(_resolve_observability_redis_uri(config), config.observability)
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                await _print_interactive_progress_line(msg.content, _thinking)

                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(msg.content, render_markdown=markdown)

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        nonlocal _thinking
                        _thinking = _ThinkingSpinner(enabled=not logs)
                        with _thinking:
                            await turn_done.wait()
                        _thinking = None

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from ithqbot.channels.registry import discover_all
    from ithqbot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from ithqbot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # ithqbot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall ithqbot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(
            [npm_path, "install"],
            cwd=user_bridge,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

        console.print("  Building...")
        subprocess.run(
            [npm_path, "run", "build"],
            cwd=user_bridge,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.TimeoutExpired as e:
        console.print("[red]Bridge setup timed out. Please retry or build the bridge manually.[/red]")
        if e.stderr:
            console.print(f"[dim]{str(e.stderr)[:500]}[/dim]")
        raise typer.Exit(1)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{str(e.stderr)[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import shutil
    import subprocess

    from ithqbot.config.loader import load_config
    from ithqbot.config.paths import get_runtime_subdir

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    wa_cfg = getattr(config.channels, "whatsapp", None) or {}
    bridge_token = wa_cfg.get("bridgeToken", "") if isinstance(wa_cfg, dict) else getattr(wa_cfg, "bridge_token", "")
    if bridge_token:
        env["BRIDGE_TOKEN"] = bridge_token
    env["AUTH_DIR"] = str(get_runtime_subdir("whatsapp-auth"))

    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js.[/red]")
        raise typer.Exit(1)

    try:
        subprocess.run([npm_path, "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins)."""
    from ithqbot.channels.registry import discover_all, discover_channel_names
    from ithqbot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled", style="green")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def observability(
    account_id: str = typer.Option(..., "--account-id", help="Account ID"),
    bot_id: str | None = typer.Option(None, "--bot-id", help="Optional bot ID for per-bot view"),
    audit_limit: int = typer.Option(20, "--audit-limit", help="Recent audit log count"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    cfg = _load_runtime_config(config, workspace=None)
    configure_observability(_resolve_observability_redis_uri(cfg), cfg.observability)
    stats = get_account_llm_stats(account_id, bot_id=bot_id)
    audits = get_recent_audit_logs(account_id, limit=audit_limit, bot_id=bot_id)

    title = f"{account_id}" if not bot_id else f"{account_id} / {bot_id}"
    console.print(f"{__logo__} Observability ({title})\n")
    table = Table(title="LLM Usage by Source")
    table.add_column("Source", style="cyan")
    table.add_column("Requests", style="green")
    table.add_column("Req Tokens", style="yellow")
    table.add_column("Resp Tokens", style="yellow")
    table.add_column("Total Tokens", style="magenta")
    for source in ("bot", "skill", "tool", "mcp"):
        row = stats.get(source) or {}
        table.add_row(
            source,
            str(row.get("request_count", 0)),
            str(row.get("request_tokens", 0)),
            str(row.get("response_tokens", 0)),
            str(row.get("total_tokens", 0)),
        )
    console.print(table)

    console.print("\nRecent Audit Logs")
    for event in audits:
        ts = event.get("ts")
        if isinstance(ts, (int, float)):
            ts_text = str(int(ts))
        else:
            ts_text = "-"
        console.print(
            f"[dim]{ts_text}[/dim] "
            f"[cyan]{event.get('call_type', '-')}/{event.get('name', '-')}[/cyan] "
            f"[green]{event.get('phase', '-') }[/green]"
        )


@app.command()
def status():
    """Show ithqbot status."""
    from ithqbot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} ithqbot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from ithqbot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from ithqbot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    console.print("[red]GitHub Copilot 登录已禁用：运行时已移除相关依赖。[/red]")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
# ============================================================================
# Config Management
# ============================================================================

config_app = typer.Typer(help="Manage ithqbot configuration.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    bot_id: str | None = typer.Option(None, "--bot-id", "-b", help="Bot ID to load"),
):
    """Show current configuration."""
    cfg = _load_runtime_config(config, bot_id=bot_id)
    console.print(cfg.model_dump(by_alias=True))


@config_app.command("sync")
def config_sync(
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    target_bot_id: str | None = typer.Option(None, "--target-bot-id", help="Target Bot ID"),
    to_redis: str | None = typer.Option(None, "--to-redis", help="Redis URI to sync to"),
    to_sql: str | None = typer.Option(None, "--to-sql", help="SQL URI to sync to"),
):
    """Sync local configuration to a remote store."""
    from ithqbot.config.loader import load_config, save_config
    from ithqbot.config.schema import ConfigStoreType
    from ithqbot.config.store import RedisConfigStore, SqlConfigStore

    # Load source config (always from file for sync source)
    source_cfg = load_config(config_path=Path(config) if config else None)
    bot_id = target_bot_id or source_cfg.bot.id

    if to_redis:
        store = RedisConfigStore(to_redis)
        store.save(bot_id, source_cfg.model_dump(by_alias=True))
        console.print(f"[green]✓[/green] Config for bot [cyan]{bot_id}[/cyan] synced to Redis: {to_redis}")
        console.print(f"\nNext: Set [bold]ITHQBOT_BOOTSTRAP_CONFIG_STORE_TYPE=redis[/bold] and [bold]ITHQBOT_BOOTSTRAP_CONFIG_STORE_URI={to_redis}[/bold]")
    elif to_sql:
        store = SqlConfigStore(to_sql)
        store.save(bot_id, source_cfg.model_dump(by_alias=True))
        console.print(f"[green]✓[/green] Config for bot [cyan]{bot_id}[/cyan] synced to SQL: {to_sql}")
        console.print(f"\nNext: Set [bold]ITHQBOT_BOOTSTRAP_CONFIG_STORE_TYPE=sql[/bold] and [bold]ITHQBOT_BOOTSTRAP_CONFIG_STORE_URI={to_sql}[/bold]")
    else:
        console.print("[yellow]Please specify --to-redis or --to-sql[/yellow]")
