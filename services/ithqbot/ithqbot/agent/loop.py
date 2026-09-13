"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from ithqbot.agent.context import ContextBuilder
from ithqbot.agent.memory import MemoryConsolidator
from ithqbot.agent.model_router import ModelRouter
from ithqbot.config.schema import RoutePurpose
from ithqbot.agent.runtime import (
    AgentPlanner,
    AgentPolicy,
    CancellationTokenSource,
    ExecutorConfig,
    ExecutorState,
    HumanInteractionRequest,
    HumanInteractionResponse,
    PlannerIterationState,
    RuntimeCheckpoint,
    RuntimeCursor,
    RuntimeStatus,
    ToolCallPlan,
    ToolCallExecutor,
    ToolRegistrar,
    apply_tool_registrars,
    build_default_tool_registrars,
    create_runtime_checkpoint_store,
)
from ithqbot.agent.skills import BUILTIN_SKILLS_DIR
from ithqbot.agent.tools.base import ToolResult
from ithqbot.agent.tools.message import MessageTool
from ithqbot.agent.tools.protocol import ToolRegistryProtocol
from ithqbot.agent.tools.registry import ToolRegistry
from ithqbot.bus.events import InboundMessage, OutboundMessage
from ithqbot.bus.queue import MessageBus
from ithqbot.file_api import FileService, FileServiceConfig
from ithqbot.observability import build_routing_observability_payload, record_trace_event
from ithqbot.providers.base import LLMProvider, ToolCallRequest
from ithqbot.session.manager import Session, SessionManager
from ithqbot.utils.helpers import estimate_prompt_tokens_chain
from ithqbot.providers.base import reset_turn_usage, take_turn_usage

if TYPE_CHECKING:
    from ithqbot.config.schema import (
        AgentRoutingConfig,
        BotGuardrailsConfig,
        BotGuardrailsPolicy,
        ChannelsConfig,
        SkillsConfig,
        ToolsConfig,
        WebSearchConfig,
    )
    from ithqbot.cron.service import CronService

class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 16_000
    _MAX_HISTORY_MESSAGES = 60
    _DEFAULT_MODEL_MAX_INPUT_TOKENS = 16_384
    _DEFAULT_PROMPT_BUDGET_RATIO = 0.9

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: WebSearchConfig | None = None,
        web_proxy: str | None = None,
        minio_config: MinioConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        bot_config: dict[str, Any] | None = None,
        routing_config: AgentRoutingConfig | None = None,
        bot_guardrails_config: BotGuardrailsConfig | None = None,
        tools_config: ToolsConfig | None = None,
        skills_config: SkillsConfig | None = None,
        memory_store_uri: str | None = None,
        session_store_uri: str | None = None,
        require_external_memory_store: bool = False,
        require_external_session_store: bool = False,
        config: Any | None = None,
        provider_factory: Callable[[str], LLMProvider] | None = None,
        tool_registry: ToolRegistryProtocol | None = None,
        tool_registrars: list[ToolRegistrar] | None = None,
        planner: AgentPlanner | None = None,
        executor: ToolCallExecutor | None = None,
        executor_config: ExecutorConfig | None = None,
        policy: AgentPolicy | None = None,
    ):
        from ithqbot.config.schema import (
            AgentRoutingConfig,
            BotGuardrailsConfig,
            MinioConfig,
            SkillsConfig,
            ToolsConfig,
            WebSearchConfig,
        )

        self.bus = bus
        self.channels_config = channels_config
        self.bot_config = bot_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.model_max_input_tokens = self._read_model_max_input_tokens()
        self.prompt_token_budget_ratio = self._read_prompt_budget_ratio()
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.minio_config = minio_config or MinioConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.routing_config = routing_config or AgentRoutingConfig()
        self.bot_guardrails_config = bot_guardrails_config or BotGuardrailsConfig()
        self.tools_config = tools_config or ToolsConfig()
        self.skills_config = skills_config or SkillsConfig()
        self.memory_store_uri = memory_store_uri
        self.session_store_uri = session_store_uri
        self.require_external_memory_store = require_external_memory_store
        self.require_external_session_store = require_external_session_store
        self.config = config
        self.provider_factory = provider_factory
        if config is not None:
            self.file_service = FileService.from_runtime_config(config)
        else:
            file_api_cfg = getattr(self.tools_config, "file_api", None)
            self.file_service = FileService(
                config=FileServiceConfig(
                    endpoint=self.minio_config.endpoint,
                    access_key=self.minio_config.access_key,
                    secret_key=self.minio_config.secret_key,
                    bucket=self.minio_config.bucket,
                    secure=bool(getattr(self.minio_config, "secure", False)),
                    max_file_size_bytes=int(
                        getattr(file_api_cfg, "max_file_size_bytes", 10 * 1024 * 1024)
                    ),
                    download_url_ttl_seconds=int(
                        getattr(file_api_cfg, "download_url_ttl_seconds", 600)
                    ),
                    metadata_prefix=str(getattr(file_api_cfg, "metadata_prefix", "_meta/files")),
                )
            )
        self._tool_enabled_patterns = list(self.tools_config.enabled or ["*"])
        self._tool_disabled_patterns = list(self.tools_config.disabled or [])
        self._bot_guardrails_history: list[dict[str, Any]] = []
        self._routing_metrics: dict[str, dict[str, int]] = {}
        configured_bot_id = self._resolve_active_bot_id({"bot_id": self.bot_config.get("id")} if isinstance(self.bot_config, dict) else None)
        initial_enabled_skills = self._resolve_enabled_skills(configured_bot_id)

        self.context = ContextBuilder(
            workspace,
            enabled_skills=initial_enabled_skills,
            memory_store_uri=self.memory_store_uri,
            allow_local_memory_fallback=not self.require_external_memory_store,
        )
        self.skills = self.context.skills
        if session_manager:
            self.sessions = session_manager
        else:
            from ithqbot.session.factory import create_session_store
            self.sessions = SessionManager(
                workspace,
                store=create_session_store(
                    workspace,
                    config_uri=self.session_store_uri,
                    require_external_store=self.require_external_session_store,
                ),
            )
        self.tools: ToolRegistryProtocol = tool_registry if tool_registry is not None else ToolRegistry()

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_queues: dict[str, asyncio.Queue] = {}
        self._session_locks: dict[str, bool] = {}
        self._runtime_controls: dict[str, CancellationTokenSource] = {}
        self._runtime_checkpoint_store = create_runtime_checkpoint_store(
            self.workspace,
            self.session_store_uri or self.memory_store_uri,
        )
        self._graph_state_manager = None
        self._graph_executor = None
        self._graph_planner = None
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            memory_store_uri=self.memory_store_uri,
            allow_local_memory_fallback=not self.require_external_memory_store,
        )
        router_model_name = str(self.routing_config.router_model or self.model).strip()
        router_provider = (
            (self.provider_factory(router_model_name) if self.provider_factory else None)
            or provider
        )
        self.model_router = ModelRouter(
            provider=router_provider,
            default_model=self.model,
            routing=self.routing_config,
            generation=router_provider.generation,
        )
        self.policy = policy if policy is not None else AgentPolicy(self)
        self.planner = planner if planner is not None else AgentPlanner(self)
        self.executor = executor if executor is not None else ToolCallExecutor(self, config=executor_config)
        self._tool_registrars = list(
            tool_registrars if tool_registrars is not None else build_default_tool_registrars(self)
        )
        self._register_default_tools()
        self._apply_tool_registration_filter()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        apply_tool_registrars(self.tools, self._tool_registrars)

    @staticmethod
    def _resolve_workspace_like_path(base_workspace: Path, configured: str | None) -> Path:
        if not configured:
            return base_workspace
        raw = Path(configured).expanduser()
        return raw.resolve() if raw.is_absolute() else (base_workspace / raw).resolve()

    def _resolve_tool_workspaces(self, workspace: Path) -> tuple[Path, Path]:
        fs_workspace = self._resolve_workspace_like_path(workspace, self.tools_config.file_root)
        minio_workspace = self._resolve_workspace_like_path(workspace, self.tools_config.minio_workspace)
        return fs_workspace, minio_workspace

    @staticmethod
    def _read_model_max_input_tokens() -> int:
        raw = os.getenv("ITHQBOT_MODEL_MAX_INPUT_TOKENS", "").strip()
        if not raw:
            return AgentLoop._DEFAULT_MODEL_MAX_INPUT_TOKENS
        try:
            parsed = int(raw)
            return parsed if parsed > 0 else AgentLoop._DEFAULT_MODEL_MAX_INPUT_TOKENS
        except ValueError:
            return AgentLoop._DEFAULT_MODEL_MAX_INPUT_TOKENS

    @staticmethod
    def _read_prompt_budget_ratio() -> float:
        raw = os.getenv("ITHQBOT_PROMPT_BUDGET_RATIO", "").strip()
        if not raw:
            return AgentLoop._DEFAULT_PROMPT_BUDGET_RATIO
        try:
            parsed = float(raw)
            if 0.1 <= parsed <= 1.0:
                return parsed
        except ValueError:
            pass
        return AgentLoop._DEFAULT_PROMPT_BUDGET_RATIO

    def _prompt_budget_tokens(self) -> int:
        hard_cap = self.model_max_input_tokens
        if hard_cap <= 0:
            return 0
        ratio_budget = max(1, int(hard_cap * self.prompt_token_budget_ratio))
        return min(hard_cap, ratio_budget)

    def _estimate_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> tuple[int, str]:
        return estimate_prompt_tokens_chain(
            self.provider,
            model or self.model,
            messages,
            tools,
        )

    def _trim_messages_to_budget(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        model: str,
        budget_tokens: int,
    ) -> tuple[list[dict[str, Any]], int, str, str]:
        estimated, source = self._estimate_prompt_tokens(messages, tools, model)
        if estimated <= 0 or estimated <= budget_tokens:
            return messages, estimated, source, "none"

        system_message = messages[0] if messages and messages[0].get("role") == "system" else None
        for idx in range(1, max(1, len(messages) - 1)):
            if messages[idx].get("role") != "user":
                continue
            candidate = ([system_message] if isinstance(system_message, dict) else []) + messages[idx:]
            repaired = self._repair_messages_for_retry(candidate)
            cand_estimated, cand_source = self._estimate_prompt_tokens(repaired, tools, model)
            if cand_estimated > 0 and cand_estimated <= budget_tokens:
                return repaired, cand_estimated, cand_source, "drop_earliest_turns"

        minimal = self._minimal_messages_for_retry(messages)
        minimal_estimated, minimal_source = self._estimate_prompt_tokens(minimal, tools, model)
        if minimal_estimated > 0 and minimal_estimated <= budget_tokens:
            return minimal, minimal_estimated, minimal_source, "minimal_retry_context"
        return messages, estimated, source, "unfit"

    def _build_skill_context(
        self,
        route_metadata: dict[str, Any] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        active_bot_id: str | None = None,
        parent_run_id: str | None = None,
        cancellation_token: Any | None = None,
    ):
        from ithqbot.agent.skills.base import SkillContext

        metadata = dict(route_metadata) if isinstance(route_metadata, dict) else {}

        async def _skill_progress_callback(*args: Any, **kwargs: Any):
            if not on_progress:
                return
            progress_percent = kwargs.pop("progress_percent", None)
            progress_kind = kwargs.pop("progress_kind", None)
            progress_stage = kwargs.pop("progress_stage", None)
            status_event = kwargs.pop("status_event", "processing")
            interaction = kwargs.pop("interaction", None)
            files = kwargs.pop("files", None)
            content_type = kwargs.pop("content_type", None)
            tool_name = kwargs.pop("tool_name", None)
            skill_name = kwargs.pop("skill_name", None)
            call_type = kwargs.pop("call_type", None)
            status_details = kwargs.pop("status_details", None)
            message = ""

            if args and isinstance(args[0], int):
                progress_percent = args[0]
                if len(args) > 1 and isinstance(args[1], str):
                    progress_stage = args[1]
                if len(args) > 2 and isinstance(args[2], str):
                    message = args[2]
            elif args and isinstance(args[0], str):
                message = args[0]
            else:
                message = str(kwargs.pop("message", ""))

            await self._emit_progress(
                on_progress,
                message,
                progress_percent=progress_percent,
                progress_kind=progress_kind,
                progress_stage=progress_stage,
                status_event=status_event,
                interaction=interaction,
                files=files,
                content_type=content_type,
                tool_name=tool_name,
                skill_name=skill_name,
                call_type=call_type,
                status_details=status_details if isinstance(status_details, dict) else None,
            )

        provider_factory = self.provider_factory or (lambda _model: self.provider)
        return SkillContext(
            tenant_id=str(metadata.get("tenant_id") or "default"),
            account_id=str(metadata.get("account_id") or "anonymous"),
            chat_id=str(metadata.get("chat_id") or "default"),
            bot_id=str(active_bot_id or metadata.get("bot_id") or "default"),
            channel_user_id=str(metadata.get("channel_user_id") or ""),
            client_id=str(metadata.get("client_id") or "web"),
            trace_id=str(metadata.get("trace_id") or ""),
            request_msg_id=str(metadata.get("msg_id") or metadata.get("request_msg_id") or ""),
            parent_run_id=str(parent_run_id or ""),
            metadata=metadata,
            config=self.config,
            provider_factory=provider_factory,
            emit_callback=_skill_progress_callback,
            file_service=self.file_service,
            cancellation_token=cancellation_token,
        )

    def _resolve_graph_store_uri(self) -> str | None:
        for candidate in [self.session_store_uri, self.memory_store_uri]:
            if isinstance(candidate, str) and candidate.startswith(("postgresql://", "postgres://", "postgresql+")):
                return candidate
        return None

    def _get_graph_state_manager(self):
        if self._graph_state_manager is not None:
            return self._graph_state_manager
        from ithqbot.graph.state_manager import InMemoryStateManager, StateManager

        graph_store_uri = self._resolve_graph_store_uri()
        if graph_store_uri:
            self._graph_state_manager = StateManager(graph_store_uri)
        elif self.require_external_session_store or self.require_external_memory_store:
            raise RuntimeError(
                "Graph runtime requires a PostgreSQL-backed sessionStoreUri or memoryStoreUri when external persistence is enforced"
            )
        else:
            self._graph_state_manager = InMemoryStateManager()
        return self._graph_state_manager

    def _get_graph_search_dirs(self) -> list[Path]:
        from ithqbot.graph.loader import get_graph_search_dirs

        builtin_graphs_dir = BUILTIN_SKILLS_DIR.parent / "graphs"
        return get_graph_search_dirs(self.workspace, builtin_graphs_dir, BUILTIN_SKILLS_DIR)

    def load_graph_by_id(self, graph_id: str):
        from ithqbot.graph.loader import load_graph_by_id

        return load_graph_by_id(graph_id, self._get_graph_search_dirs())

    def _get_graph_executor(self):
        if self._graph_executor is not None:
            return self._graph_executor
        from ithqbot.graph import GraphExecutor

        self._graph_executor = GraphExecutor(
            self._get_graph_state_manager(),
            self,
            graph_loader=self.load_graph_by_id,
        )
        return self._graph_executor

    def _get_graph_planner(self):
        if self._graph_planner is not None:
            return self._graph_planner
        from ithqbot.planner import GraphPlanner, SkillInfo

        async def _planner_llm(prompt: str) -> str:
            route_profile = await self.model_router.decide(
                prompt,
                {"channel": "graph_planner"},
                purpose=RoutePurpose.PLANNER,
            )
            provider_factory = self.provider_factory or (lambda _model: self.provider)
            provider = provider_factory(route_profile.active_model or self.model) or self.provider
            response = await provider.chat_with_retry(
                messages=[{"role": "user", "content": prompt}],
                model=route_profile.active_model or self.model,
                max_tokens=route_profile.max_tokens,
                reasoning_effort=route_profile.reasoning_effort,
            )
            return response.content or ""

        loop = self

        class _PlannerSkillLoader:
            @staticmethod
            def _normalize_semantic_list(value: Any) -> list[str]:
                if not isinstance(value, list):
                    return []
                normalized: list[str] = []
                for item in value:
                    if isinstance(item, str):
                        name = item.strip()
                    elif isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                    else:
                        name = ""
                    if name and name not in normalized:
                        normalized.append(name)
                return normalized

            def load_skills(self):
                descriptions: dict[str, str] = {}
                input_schemas: dict[str, dict[str, Any]] = {}
                output_schemas: dict[str, dict[str, Any]] = {}
                semantics: dict[str, dict[str, list[str]]] = {}
                capabilities: dict[str, list[str]] = {}
                tags_map: dict[str, list[str]] = {}
                levels: dict[str, str] = {}
                planners: dict[str, dict[str, list[str]]] = {}
                idempotency: dict[str, bool] = {}
                retryability: dict[str, bool] = {}
                costs: dict[str, dict[str, Any]] = {}
                latencies: dict[str, dict[str, Any]] = {}
                for tool_def in loop.tools.get_definitions():
                    function = tool_def.get("function", {}) if isinstance(tool_def, dict) else {}
                    name = function.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    descriptions[name] = str(function.get("description") or name)
                    tool_contract = {}
                    tool_def_loader = getattr(loop.skills, "_load_tool_def", None)
                    if callable(tool_def_loader):
                        try:
                            tool_contract = tool_def_loader(name) or {}
                        except Exception:
                            tool_contract = {}
                    contract_input = tool_contract.get("input_schema")
                    if isinstance(contract_input, dict):
                        input_schemas[name] = dict(contract_input)
                    else:
                        parameters = function.get("parameters")
                        input_schemas[name] = parameters if isinstance(parameters, dict) else {}
                    if isinstance(tool_contract.get("output_schema"), dict):
                        output_schemas[name] = dict(tool_contract["output_schema"])
                    semantic = tool_contract.get("semantic")
                    if isinstance(semantic, dict):
                        semantics[name] = {
                            key: self._normalize_semantic_list(value)
                            for key, value in semantic.items()
                            if key in {"produces", "consumes"} and isinstance(value, list)
                        }
                    if isinstance(tool_contract.get("idempotent"), bool):
                        idempotency[name] = tool_contract["idempotent"]
                    if isinstance(tool_contract.get("retryable"), bool):
                        retryability[name] = tool_contract["retryable"]
                    if isinstance(tool_contract.get("cost"), dict):
                        costs[name] = dict(tool_contract["cost"])
                    contract_latency = tool_contract.get("latency")
                    if isinstance(contract_latency, dict):
                        latencies[name] = dict(contract_latency)
                    elif isinstance(tool_contract.get("sla"), dict):
                        sla = tool_contract["sla"]
                        normalized_latency: dict[str, Any] = {}
                        if isinstance(sla.get("latency_ms"), int):
                            normalized_latency["expected_ms"] = sla["latency_ms"]
                        if isinstance(sla.get("availability"), str):
                            normalized_latency["availability"] = sla["availability"]
                        if normalized_latency:
                            latencies[name] = normalized_latency
                    metadata_getter = getattr(loop.skills, "get_skill_metadata", None)
                    parser = getattr(loop.skills, "_parse_ithqbot_metadata", None)
                    skill_meta: dict[str, Any] = {}
                    if callable(metadata_getter):
                        try:
                            raw_meta = metadata_getter(name) or {}
                        except Exception:
                            raw_meta = {}
                        if isinstance(raw_meta, dict):
                            meta_payload = raw_meta.get("metadata", raw_meta)
                            if callable(parser):
                                parsed_meta = parser(meta_payload)
                                skill_meta = parsed_meta if isinstance(parsed_meta, dict) else {}
                            elif isinstance(meta_payload, dict):
                                skill_meta = meta_payload
                    capabilities[name] = [
                        item for item in skill_meta.get("capability", []) if isinstance(item, str) and item.strip()
                    ]
                    tags_map[name] = [item for item in skill_meta.get("tags", []) if isinstance(item, str) and item.strip()]
                    level = skill_meta.get("level")
                    if isinstance(level, str) and level.strip():
                        levels[name] = level.strip().lower()
                    planner_meta = skill_meta.get("planner")
                    if isinstance(planner_meta, dict):
                        planners[name] = {
                            key: [
                                item
                                for item in value
                                if isinstance(item, str) and item.strip()
                            ]
                            for key, value in planner_meta.items()
                            if key in {"input_from", "output_to", "incompatible_with", "preferred_after"}
                            and isinstance(value, list)
                        }
                    if name not in idempotency and isinstance(skill_meta.get("idempotent"), bool):
                        idempotency[name] = skill_meta["idempotent"]
                    if name not in retryability and isinstance(skill_meta.get("retryable"), bool):
                        retryability[name] = skill_meta["retryable"]
                    if name not in costs and isinstance(skill_meta.get("cost"), dict):
                        costs[name] = dict(skill_meta["cost"])
                    if name not in latencies and isinstance(skill_meta.get("latency"), dict):
                        latencies[name] = dict(skill_meta["latency"])

                skills = []
                for name in sorted(descriptions):
                    description = descriptions[name]
                    skill_description_getter = getattr(loop.skills, "_get_skill_description", None)
                    if callable(skill_description_getter):
                        try:
                            description = skill_description_getter(name) or description
                        except Exception:
                            description = description
                    skills.append(
                        SkillInfo(
                            name=name,
                            description=description,
                            input_schema=input_schemas.get(name, {}),
                            output_schema=output_schemas.get(name, {}),
                            semantic=semantics.get(name, {}),
                            capability=capabilities.get(name, []),
                            tags=tags_map.get(name, []),
                            level=levels.get(name, "atomic"),
                            planner=planners.get(name, {}),
                            idempotent=idempotency.get(name),
                            retryable=retryability.get(name),
                            cost=costs.get(name, {}),
                            latency=latencies.get(name, {}),
                        )
                    )
                return skills

        self._graph_planner = GraphPlanner(llm=_planner_llm, skill_loader=_PlannerSkillLoader())
        return self._graph_planner

    async def run_skill(self, skill_name: str, payload: dict[str, Any], skill_context: Any) -> Any:
        result = await self._invoke_tool_via_plan(
            tool_name=skill_name,
            tool_args=payload,
            skill_context=skill_context,
        )
        if not result.success:
            raise RuntimeError(result.content)
        parsed = self._extract_json_object(result.content)
        if isinstance(parsed, dict):
            parsed = await self._normalize_payload_files(parsed, skill_context=skill_context)
            return parsed
        return result.content

    async def _invoke_tool_via_plan(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        skill_context: Any,
        route_metadata: dict[str, Any] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        active_bot_id: str | None = None,
        iteration: int = 0,
        record_assistant_tool_call: bool = False,
        enforce_guardrails: bool = True,
    ) -> ToolResult:
        resolved_bot_id = active_bot_id or self._resolve_active_bot_id(route_metadata)
        guardrails_policy = self._resolve_bot_guardrails_policy(route_metadata)[1]
        if enforce_guardrails and route_metadata is not None and not self._guardrails_is_tool_allowed(
            tool_name,
            resolved_bot_id,
            guardrails_policy,
            route_metadata,
        ):
            return ToolResult(
                content=f"错误：工具“{tool_name}”因安全策略限制，暂不可用。",
                success=False,
                error="guardrails_blocked",
            )
        tool_call_id = str(getattr(skill_context, "parent_run_id", "") or f"call_{uuid.uuid4().hex}")
        tool_plan = ToolCallPlan.single(
            tool_name=tool_name,
            arguments=tool_args,
            tool_call_id=tool_call_id,
        )
        execution_result = await self.executor.execute_plan(
            plan=tool_plan,
            response_content=None,
            response_reasoning_content=None,
            response_thinking_blocks=None,
            messages=[],
            skill_context=skill_context,
            on_progress=on_progress,
            route_metadata=route_metadata,
            active_bot_id=resolved_bot_id,
            guardrails_policy=guardrails_policy,
            iteration=iteration,
            state=ExecutorState(),
            record_assistant_tool_call=record_assistant_tool_call,
        )
        plan_record = tool_plan.to_record(
            inputs={
                "tool_name": tool_name,
                "tool_args": dict(tool_args),
                "iteration": iteration,
                "active_bot_id": resolved_bot_id,
            },
            outputs=execution_result.to_dict(),
        )
        if isinstance(route_metadata, dict):
            route_metadata["_plan_record"] = plan_record
        return execution_result.node_results.get(
            tool_call_id,
            ToolResult(content="", success=False, error="missing_node_result"),
        )

    async def run_graph(
        self,
        graph,
        *,
        metadata: dict[str, Any] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        run_id: str | None = None,
        initial_state: dict[str, Any] | None = None,
    ):
        from ithqbot.graph import GraphRun

        skill_context = self._build_skill_context(metadata, on_progress)
        run = GraphRun(
            run_id=run_id or f"graph-{uuid.uuid4().hex}",
            graph_id=graph.graph_id,
            status="pending",
            state=dict(initial_state or {}),
        )
        return await self._get_graph_executor().run(graph, run, skill_context)

    async def run_graph_by_id(
        self,
        graph_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        run_id: str | None = None,
        initial_state: dict[str, Any] | None = None,
    ):
        graph = self.load_graph_by_id(graph_id)
        return await self.run_graph(
            graph,
            metadata=metadata,
            on_progress=on_progress,
            run_id=run_id,
            initial_state=initial_state,
        )

    async def plan_graph(
        self,
        query: str,
        *,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        from ithqbot.planner import safe_plan

        planner = self._get_graph_planner()
        await self._emit_progress(
            on_progress,
            "正在规划技能图",
            progress_percent=10,
            progress_kind="planner",
            progress_stage="graph_planning",
            call_type="graph",
            status_details={"planner": {"query": query[:200]}},
        )
        direct_graph = self._build_direct_skill_compliance_graph(query)
        if direct_graph is not None:
            record_trace_event(
                event_name="graph.planner.shortcut",
                phase="point",
                status="ok",
                component="graph_planner",
                source="bot",
                event_type="graph",
                details={"reason": "skill_compliance_intent", "query": query[:200]},
            )
            await self._emit_progress(
                on_progress,
                "识别到技能合规检查请求，已直连 check_skill",
                progress_percent=20,
                progress_kind="planner",
                progress_stage="graph_ready",
                call_type="graph",
                status_details={
                    "graph": {
                        "graph_id": direct_graph.get("graph_id"),
                        "node_count": len(direct_graph.get("nodes", [])),
                        "edge_count": len(direct_graph.get("edges", [])),
                    }
                },
            )
            return direct_graph
        graph_dict = await safe_plan(planner, query)
        await self._emit_progress(
            on_progress,
            "技能图已生成，开始执行",
            progress_percent=20,
            progress_kind="planner",
            progress_stage="graph_ready",
            call_type="graph",
            status_details={
                "graph": {
                    "graph_id": graph_dict.get("graph_id"),
                    "node_count": len(graph_dict.get("nodes", [])),
                    "edge_count": len(graph_dict.get("edges", [])),
                }
            },
        )
        return graph_dict

    def _build_direct_skill_compliance_graph(self, query: str) -> dict[str, Any] | None:
        if "check_skill" not in self.tools.tool_names:
            return None
        if not self._is_skill_compliance_query(query):
            return None
        target_skill = self._extract_target_skill_for_compliance(query)
        if not target_skill:
            return None
        strict_mode = bool(re.search(r"严格|strict", query, re.IGNORECASE))
        return {
            "graph_id": "skill_compliance_check",
            "nodes": [
                {
                    "id": "step1",
                    "skill": "check_skill",
                    "input": {"skill_name": target_skill, "strict": strict_mode},
                }
            ],
            "edges": [],
        }

    def _is_skill_compliance_query(self, query: str) -> bool:
        normalized = query.strip()
        if not normalized:
            return False
        has_skill_scope = bool(
            re.search(
                r"(?:(?<![a-z0-9_])skill(?![a-z0-9_])|技能|SKILL_STANDARDS|skill_standards|SKILL\.md)",
                normalized,
                re.IGNORECASE,
            )
        )
        if not has_skill_scope:
            return False
        return bool(
            re.search(
                r"(符合规范|是否符合|合规|规范检查|标准检查|审计|compliance|conform|standard)",
                normalized,
                re.IGNORECASE,
            )
        )

    def _extract_target_skill_for_compliance(self, query: str) -> str | None:
        available_skills = sorted({name for name in self.tools.tool_names if isinstance(name, str)}, key=len, reverse=True)
        for skill_name in available_skills:
            pattern = rf"(?<![a-z0-9_]){re.escape(skill_name)}(?![a-z0-9_])"
            if re.search(pattern, query, re.IGNORECASE):
                return skill_name

        hinted = re.search(r"([a-z][a-z0-9_-]{1,63})\s*skill", query, re.IGNORECASE)
        if hinted:
            hinted_name = hinted.group(1).lower()
            if hinted_name in available_skills:
                return hinted_name
        return None

    @staticmethod
    def _is_file_summary_query(query: str) -> bool:
        normalized = (query or "").strip()
        if not normalized:
            return False
        has_summary_intent = bool(
            re.search(r"(总结|摘要|概述|提炼|归纳|summari[sz]e|summary)", normalized, re.IGNORECASE)
        )
        if not has_summary_intent:
            return False
        return bool(
            re.search(r"(文件|文档|附件|file|doc|pdf|word|刚刚|刚才|这个|该)", normalized, re.IGNORECASE)
        )

    @staticmethod
    def _looks_like_image_name(name: str) -> bool:
        lowered = str(name or "").strip().lower()
        return lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".svg"))

    @classmethod
    def _has_visual_inputs(
        cls,
        media: list[str] | None,
        metadata: dict[str, Any] | None,
    ) -> bool:
        for item in media or []:
            mime, _ = mimetypes.guess_type(str(item))
            if (mime and mime.startswith("image/")) or cls._looks_like_image_name(str(item)):
                return True
        if not isinstance(metadata, dict):
            return False
        content_type = str(metadata.get("content_type") or "").strip().lower()
        if content_type == "image" or content_type.startswith("image/"):
            return True
        entries = cls._collect_file_entries_from_metadata(metadata)
        for entry in entries:
            mime = str(entry.get("mime") or entry.get("content_type") or "").strip().lower()
            if mime == "image" or mime.startswith("image/"):
                return True
            name = str(
                entry.get("name")
                or entry.get("filename")
                or entry.get("original_file_name")
                or ""
            ).strip()
            if cls._looks_like_image_name(name):
                return True
        return False

    def _infer_route_purpose(
        self,
        *,
        content: str,
        media: list[str] | None,
        metadata: dict[str, Any] | None,
    ) -> RoutePurpose:
        if self._has_visual_inputs(media, metadata):
            return RoutePurpose.VISION
        if self.tools.get("file_summary_skill") and self._is_file_summary_query(content):
            return RoutePurpose.EXTRACTION
        return RoutePurpose.PLANNER

    @staticmethod
    def _collect_file_entries_from_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(metadata, dict):
            return []
        entries: list[dict[str, Any]] = []
        for key in ("files", "attachments"):
            raw = metadata.get(key)
            if isinstance(raw, list):
                entries.extend([item for item in raw if isinstance(item, dict)])
        file_meta = metadata.get("file_meta")
        if isinstance(file_meta, dict):
            entries.append(file_meta)
        return entries

    @staticmethod
    def _extract_file_id_from_interaction_context(interaction_context: dict[str, Any] | None) -> str:
        if not isinstance(interaction_context, dict):
            return ""
        for key in ("file_id", "source_file_id", "selected_file_id"):
            raw = interaction_context.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        files = interaction_context.get("files")
        if isinstance(files, list):
            for item in files:
                if isinstance(item, dict):
                    raw = item.get("file_id")
                    if isinstance(raw, str) and raw.strip():
                        return raw.strip()
        return ""

    @staticmethod
    def _extract_recent_file_id_from_history(history: list[dict[str, Any]]) -> str:
        for item in reversed(history[-12:]):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "")
            if not content:
                continue
            match = re.search(r"\b(f_[a-zA-Z0-9_]{8,})\b", content)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _extract_filename_hints(query: str) -> list[str]:
        text = str(query or "")
        if not text.strip():
            return []
        matches = re.findall(
            r"([^\s\"'`“”‘’<>]+?\.(?:docx?|pdf|txt|md|pptx?|xlsx?|csv))",
            text,
            flags=re.IGNORECASE,
        )
        hints: list[str] = []
        seen: set[str] = set()
        for raw in matches:
            candidate = str(raw or "").strip().strip(".,;:!?()[]{}")
            if not candidate:
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            hints.append(candidate)
        return hints

    @staticmethod
    def _normalize_filename_for_match(name: str) -> str:
        return re.sub(r"\s+", "", str(name or "").strip().lower())

    def _recent_file_name_candidates(self, item: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        for raw in (
            item.get("original_file_name"),
            item.get("name"),
            item.get("file_name"),
        ):
            normalized = self._normalize_filename_for_match(str(raw or ""))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
        return candidates

    async def _select_recent_file_from_storage(
        self,
        *,
        tenant_id: str,
        account_id: str,
        bot_id: str,
        query: str,
        limit: int = 30,
        default_to_latest: bool = False,
    ) -> dict[str, Any] | None:
        recent_files = await self.file_service.list(
            tenant_id=tenant_id,
            account_id=account_id,
            bot_id=bot_id,
            limit=limit,
        )
        if not recent_files:
            return None
        filename_hints = self._extract_filename_hints(query)
        if filename_hints:
            normalized_hints = [self._normalize_filename_for_match(item) for item in filename_hints]
            for recent in recent_files:
                file_id = str((recent or {}).get("file_id") or "").strip()
                if not file_id:
                    continue
                candidate_names = self._recent_file_name_candidates(recent if isinstance(recent, dict) else {})
                if any(
                    hint and (hint == name or hint in name or name in hint)
                    for hint in normalized_hints
                    for name in candidate_names
                ):
                    return recent
        if default_to_latest:
            for recent in recent_files:
                file_id = str((recent or {}).get("file_id") or "").strip()
                if file_id:
                    return recent
        return None

    async def _resolve_file_id_for_summary(
        self,
        *,
        interaction_context: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
        history: list[dict[str, Any]],
        route_metadata: dict[str, Any],
    ) -> str:
        direct_file_id = self._extract_file_id_from_interaction_context(interaction_context)
        if direct_file_id:
            return direct_file_id

        tenant_id = str(route_metadata.get("tenant_id") or "")
        account_id = str(route_metadata.get("account_id") or "")
        bot_id = str(route_metadata.get("bot_id") or "")
        default_bucket = str(getattr(self.file_service, "_bucket", "") or "")
        default_backend = str(getattr(getattr(self.file_service, "_config", None), "backend", "minio") or "minio").strip()
        for entry in self._collect_file_entries_from_metadata(metadata):
            candidate = dict(entry)
            if not str(candidate.get("storage_uri") or "").strip():
                storage = candidate.get("storage")
                if isinstance(storage, dict):
                    storage_backend = str(storage.get("backend") or default_backend).strip() or default_backend
                    storage_bucket = str(storage.get("bucket") or default_bucket).strip()
                    storage_path = str(storage.get("path") or storage.get("object_key") or "").strip().strip("/")
                    if storage_bucket and storage_path:
                        candidate["storage_uri"] = f"{storage_backend}://{storage_bucket}/{storage_path}"
                if not str(candidate.get("storage_uri") or "").strip():
                    rel_path = str(candidate.get("rel_path") or "").strip().strip("/")
                    if default_bucket and rel_path:
                        candidate["storage_uri"] = f"{default_backend}://{default_bucket}/{rel_path}"
            if not str(candidate.get("minio_uri") or "").strip():
                storage_uri = str(candidate.get("storage_uri") or "").strip()
                if storage_uri.startswith("minio://"):
                    candidate["minio_uri"] = storage_uri
            if not str(candidate.get("s3_uri") or "").strip():
                storage_uri = str(candidate.get("storage_uri") or "").strip()
                if storage_uri.startswith("s3://"):
                    candidate["s3_uri"] = storage_uri
            resolved = await self.file_service.resolve_file_id_from_legacy(
                file_entry=candidate,
                tenant_id=tenant_id,
                account_id=account_id,
                bot_id=bot_id,
            )
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()

        from_history = self._extract_recent_file_id_from_history(history)
        if from_history:
            return from_history

        query_hint = str((metadata or {}).get("_current_query") or "")
        selected_recent = await self._select_recent_file_from_storage(
            tenant_id=tenant_id,
            account_id=account_id,
            bot_id=bot_id,
            query=query_hint,
            limit=30,
            default_to_latest=True,
        )
        if selected_recent:
            fallback_id = str((selected_recent or {}).get("file_id") or "").strip()
            if fallback_id:
                return fallback_id
        return ""

    async def handle_request_via_graph(
        self,
        query: str,
        *,
        metadata: dict[str, Any] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        run_id: str | None = None,
        initial_state: dict[str, Any] | None = None,
    ):
        from ithqbot.graph import load_graph_from_dict

        graph_dict = await self.plan_graph(query, on_progress=on_progress)
        graph = load_graph_from_dict(graph_dict)
        return await self.run_graph(
            graph,
            metadata=metadata,
            on_progress=on_progress,
            run_id=run_id,
            initial_state=initial_state,
        )

    def _resolve_graph_request(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        meta = metadata if isinstance(metadata, dict) else {}
        graph_request = meta.get("graph_request")
        if isinstance(graph_request, dict):
            mode = str(graph_request.get("mode") or "").strip().lower()
            if mode in {"plan", "preview"}:
                query = str(graph_request.get("query") or content or "").strip()
                if query:
                    return {"mode": mode, "query": query, "source": "metadata"}
            if mode in {"run", "run_by_id"}:
                graph_id = str(graph_request.get("graph_id") or "").strip()
                if graph_id:
                    return {"mode": "run_by_id", "graph_id": graph_id, "source": "metadata"}

        execution_mode = str(meta.get("execution_mode") or meta.get("agent_mode") or "").strip().lower()
        if execution_mode in {"graph", "graph_plan", "planner"}:
            query = str(meta.get("graph_query") or content or "").strip()
            if query:
                return {"mode": "plan", "query": query, "source": "metadata"}
        if execution_mode in {"graph_preview", "planner_preview"}:
            query = str(meta.get("graph_query") or content or "").strip()
            if query:
                return {"mode": "preview", "query": query, "source": "metadata"}
        if execution_mode in {"graph_run", "graph_run_by_id"}:
            graph_id = str(meta.get("graph_id") or "").strip()
            if graph_id:
                return {"mode": "run_by_id", "graph_id": graph_id, "source": "metadata"}

        stripped = content.strip()
        slash_match = re.match(
            r"^/graph(?:\s+(plan|preview|run))?(?:\s+(.*))?$",
            stripped,
            re.IGNORECASE | re.DOTALL,
        )
        if slash_match:
            action = (slash_match.group(1) or "plan").strip().lower()
            payload = (slash_match.group(2) or "").strip()
            if action in {"plan", "preview"} and payload:
                return {"mode": action, "query": payload, "source": "slash"}
            if action == "run" and payload:
                return {"mode": "run_by_id", "graph_id": payload, "source": "slash"}
            return None

        graph_run_match = re.match(
            r"^(?:请)?(?:执行|运行|触发)(?:技能图|图流程|工作流图|graph)\s*(?:graph[_\s-]*id\s*[=:]\s*)?([a-zA-Z0-9._-]+)\s*$",
            stripped,
            re.IGNORECASE,
        )
        if graph_run_match:
            return {
                "mode": "run_by_id",
                "graph_id": graph_run_match.group(1).strip(),
                "source": "nl",
            }

        graph_plan_match = re.match(
            r"^(?:请)?(?:使用|用|通过)?(?:技能图|图规划|graph planner|planner|工作流图)(?:来)?(?:规划并执行|执行|处理|完成)[:：\s]+(.+)$",
            stripped,
            re.IGNORECASE | re.DOTALL,
        )
        if graph_plan_match:
            return {
                "mode": "plan",
                "query": graph_plan_match.group(1).strip(),
                "source": "nl",
            }
        return None

    def _build_graph_reply(
        self,
        run,
        *,
        preview_graph: dict[str, Any] | None = None,
    ) -> str:
        if isinstance(preview_graph, dict):
            return json.dumps(preview_graph, ensure_ascii=False, indent=2)
        summary_keys = ("final_answer", "summary", "answer", "message", "echo", "result", "output")
        state_payload = run.state if hasattr(run, "state") and isinstance(run.state, dict) else {}
        for key in summary_keys:
            value = state_payload.get(key)
            if isinstance(value, str) and value.strip():
                return (
                    f"技能图执行完成。\n"
                    f"graph_id: {run.graph_id}\n"
                    f"run_id: {run.run_id}\n"
                    f"结果: {value.strip()}"
                )
        status_text = {
            "done": "技能图执行完成。",
            "waiting": "技能图已暂停，等待用户输入。",
            "failed": "技能图执行失败。",
        }.get(getattr(run, "status", ""), "技能图执行结束。")
        if state_payload:
            compact_state = json.dumps(state_payload, ensure_ascii=False, sort_keys=True)
            if len(compact_state) > 600:
                compact_state = compact_state[:600] + "..."
            return (
                f"{status_text}\n"
                f"graph_id: {run.graph_id}\n"
                f"run_id: {run.run_id}\n"
                f"state: {compact_state}"
            )
        return f"{status_text}\ngraph_id: {run.graph_id}\nrun_id: {run.run_id}"

    def _pattern_match(self, name: str, patterns: list[str]) -> bool:
        for raw in patterns:
            if not isinstance(raw, str):
                continue
            pattern = raw.strip()
            if not pattern:
                continue
            if pattern == "*" or fnmatch.fnmatch(name, pattern):
                return True
        return False

    def _is_tool_enabled_by_config(self, tool_name: str) -> bool:
        if self._pattern_match(tool_name, self._tool_disabled_patterns):
            return False
        if not self._tool_enabled_patterns:
            return True
        return self._pattern_match(tool_name, self._tool_enabled_patterns)

    def _apply_tool_registration_filter(self) -> None:
        for name in [tool.name for tool in self.tools.list_tools()]:
            if not self._is_tool_enabled_by_config(name):
                self.tools.unregister(name)

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from ithqbot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._apply_tool_registration_filter()
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_workspace(self, workspace: Path) -> None:
        """Update workspace for all filesystem and execution tools."""
        fs_workspace, minio_workspace = self._resolve_tool_workspaces(workspace)
        for tool in self.tools.list_tools():
            name = tool.name
            if hasattr(tool, "_workspace"):
                tool._workspace = minio_workspace if name.startswith("minio_") else fs_workspace

    def _set_tool_context(self) -> None:
        return

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _build_processing_status_details(
        content: str,
        *,
        progress_stage: str | None = None,
        tool_name: str | None = None,
        skill_name: str | None = None,
        call_type: str | None = None,
        status_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details = dict(status_details or {})
        details.setdefault("presentation", "step_list")
        details.setdefault("show_progress_percent", False)
        details.setdefault("auto_collapse_on_complete", True)
        details.setdefault("collapsed_sections", {"completed": True, "pending": True})
        if isinstance(details.get("steps"), list):
            if progress_stage and not details.get("current_step_id"):
                details["current_step_id"] = progress_stage
            return details

        stage_titles = {
            "queued": "任务排队中",
            "parsing": "理解请求",
            "extracting": "整理上下文",
            "tool_call": "执行步骤",
            "skill_call": "执行技能",
            "finalizing": "整理结果",
        }
        ordered_stages = ["queued", "parsing", "extracting", "tool_call", "finalizing"]
        current_stage = progress_stage if progress_stage in ordered_stages else None
        if current_stage is None:
            step_title = content or stage_titles.get(progress_stage or "", "处理中")
            if tool_name:
                step_title = f"{step_title}：{tool_name}"
            elif skill_name:
                step_title = f"{step_title}：{skill_name}"
            return {
                **details,
                "current_step_id": progress_stage or "processing",
                "steps": [
                    {
                        "id": progress_stage or "processing",
                        "title": step_title,
                        "state": "active",
                        "expandable": False,
                        "collapsed": False,
                        "call_type": call_type,
                    }
                ],
            }

        current_index = ordered_stages.index(current_stage)
        steps: list[dict[str, Any]] = []
        for idx, stage in enumerate(ordered_stages):
            title = stage_titles[stage]
            if stage == current_stage and content:
                title = content
            elif stage in {"tool_call"} and tool_name:
                title = f"{title}：{tool_name}"
            elif stage in {"tool_call"} and skill_name:
                title = f"{title}：{skill_name}"
            state = "pending"
            if idx < current_index:
                state = "completed"
            elif idx == current_index:
                state = "active"
            steps.append(
                {
                    "id": stage,
                    "title": title,
                    "state": state,
                    "expandable": state != "active",
                    "collapsed": state != "active",
                    "call_type": call_type if stage == current_stage else None,
                }
            )
        details["current_step_id"] = current_stage
        details["steps"] = steps
        return details

    def _tool_call_type(self, tool_name: str) -> str:
        if tool_name.startswith("mcp_"):
            return "mcp"
        tool = self.tools.get(tool_name)
        module_name = getattr(tool.__class__, "__module__", "") if tool else ""
        if module_name.startswith("ithqbot.skills.dynamic.") or module_name.startswith("ithqbot.skills."):
            return "skill"
        return "tool"

    @staticmethod
    def _trace_route_identity(
        route_metadata: dict[str, Any] | None,
        active_bot_id: str | None = None,
    ) -> dict[str, Any]:
        meta = route_metadata if isinstance(route_metadata, dict) else {}
        return {
            "account_id": meta.get("account_id"),
            "tenant_id": meta.get("tenant_id"),
            "bot_id": active_bot_id,
            "channel": meta.get("channel"),
            "chat_id": meta.get("chat_id"),
            "client_id": meta.get("client_id"),
        }

    def _trace_tool_lifecycle_start(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        route_metadata: dict[str, Any] | None,
        active_bot_id: str | None,
        direct_invoke: bool = False,
        content_preview: str | None = None,
        extra_details: dict[str, Any] | None = None,
    ) -> None:
        trace_identity = self._trace_route_identity(route_metadata, active_bot_id)
        details = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "run_id": tool_call_id,
            "arguments": tool_args,
        }
        if direct_invoke:
            details["direct_invoke"] = True
        if isinstance(extra_details, dict):
            details.update(extra_details)
        record_trace_event(
            event_name="tool.call.started",
            phase="start",
            status="running",
            component="agent_loop",
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            content_preview=content_preview or json.dumps(tool_args, ensure_ascii=False),
            event_type="tool.call",
            details=details,
        )
        record_trace_event(
            event_name="skill.submitted",
            phase="request",
            status="running",
            component=tool_name,
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            content_preview=f"提交给 skill: {tool_name}",
            event_type="skill.call",
            details={
                "label": "提交给 skill",
                "tool_call_id": tool_call_id,
                "run_id": tool_call_id,
                "arguments": tool_args,
                "direct_invoke": direct_invoke,
            },
        )

    def _trace_tool_lifecycle_end(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        route_metadata: dict[str, Any] | None,
        active_bot_id: str | None,
        success: bool,
        skill_content_preview: str,
        tool_content_preview: str | None = None,
        result_payload: Any = None,
        error_text: str | None = None,
        direct_invoke: bool = False,
        extra_details: dict[str, Any] | None = None,
    ) -> None:
        trace_identity = self._trace_route_identity(route_metadata, active_bot_id)
        status = "ok" if success else "error"
        skill_label = "Skill 返回结果" if success else "Skill 执行失败"
        skill_details: dict[str, Any] = {
            "label": skill_label,
            "tool_call_id": tool_call_id,
            "run_id": tool_call_id,
            "direct_invoke": direct_invoke,
        }
        if isinstance(extra_details, dict):
            skill_details.update(extra_details)
        if success:
            skill_details["has_result"] = bool(result_payload)
        else:
            skill_details["error"] = error_text or ""
        record_trace_event(
            event_name="skill.result_received",
            phase="response",
            status=status,
            component=tool_name,
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            content_preview=skill_content_preview,
            event_type="skill.call",
            details=skill_details,
        )
        tool_details: dict[str, Any] = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "run_id": tool_call_id,
            "direct_invoke": direct_invoke,
        }
        if isinstance(extra_details, dict):
            tool_details.update(extra_details)
        if success:
            tool_details["has_payload"] = isinstance(result_payload, dict)
        else:
            tool_details["error"] = error_text or ""
        record_trace_event(
            event_name="tool.call.finished",
            phase="end",
            status=status,
            component="agent_loop",
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            content_preview=tool_content_preview if tool_content_preview is not None else skill_content_preview,
            event_type="tool.call",
            details=tool_details,
        )

    def _trace_tool_blocked(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        route_metadata: dict[str, Any] | None,
        active_bot_id: str | None,
        content_preview: str,
        extra_details: dict[str, Any] | None = None,
    ) -> None:
        trace_identity = self._trace_route_identity(route_metadata, active_bot_id)
        details: dict[str, Any] = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
        }
        if isinstance(extra_details, dict):
            details.update(extra_details)
        record_trace_event(
            event_name="tool.call.blocked",
            phase="end",
            status="blocked",
            component="agent_loop",
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            content_preview=content_preview,
            event_type="tool.call",
            details=details,
        )

    def _trace_direct_graph_shortcut_start(
        self,
        *,
        graph_spec: dict[str, Any],
        route_metadata: dict[str, Any] | None,
        active_bot_id: str | None,
        content_preview: str,
    ) -> dict[str, str]:
        trace_identity = self._trace_route_identity(route_metadata, active_bot_id)
        graph_id = str(graph_spec.get("graph_id") or "direct_skill_graph").strip() or "direct_skill_graph"
        request_msg_id = str((route_metadata or {}).get("request_msg_id") or uuid.uuid4().hex).strip()
        graph_run_id = f"{request_msg_id}:{graph_id}"
        first_node = graph_spec.get("nodes", [{}])[0] if isinstance(graph_spec.get("nodes"), list) else {}
        node_id = str(first_node.get("id") or "step1").strip() or "step1"
        skill_name = str(first_node.get("skill") or "check_skill").strip() or "check_skill"
        node_run_id = f"{graph_run_id}:{node_id}"
        topology = {
            "nodes": [
                {
                    "node_id": node_id,
                    "skill_name": skill_name,
                    "status": "running",
                }
            ],
            "edges": list(graph_spec.get("edges") or []),
        }
        record_trace_event(
            event_name="graph.run.start",
            phase="start",
            status="running",
            component="graph_executor",
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            content_preview=content_preview,
            event_type="graph",
            details={
                "graph_id": graph_id,
                "run_id": graph_run_id,
                "graph": {
                    "graph_id": graph_id,
                    "run_id": graph_run_id,
                    "status": "running",
                    "node_count": 1,
                    "completed_nodes": 0,
                    "failed_nodes": 0,
                    "waiting_nodes": 0,
                    "running_nodes": 1,
                    "pending_nodes": 0,
                    "skipped_nodes": 0,
                    "current_node_ids": [node_id],
                    "topology": topology,
                },
            },
        )
        return {
            "graph_id": graph_id,
            "graph_run_id": graph_run_id,
            "node_id": node_id,
            "node_run_id": node_run_id,
            "skill_name": skill_name,
        }

    def _trace_direct_graph_shortcut_end(
        self,
        *,
        graph_trace: dict[str, str],
        route_metadata: dict[str, Any] | None,
        active_bot_id: str | None,
        success: bool,
        content_preview: str,
        error_text: str | None = None,
    ) -> None:
        trace_identity = self._trace_route_identity(route_metadata, active_bot_id)
        node_status = "done" if success else "failed"
        graph_status = "done" if success else "failed"
        final_status = "ok" if success else "error"
        current_node_ids: list[str] = []
        completed_nodes = 1 if success else 0
        failed_nodes = 0 if success else 1
        record_trace_event(
            event_name="graph.node.done" if success else "graph.node.failed",
            phase="end",
            status=final_status,
            component=graph_trace["skill_name"],
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            parent_run_id=graph_trace["graph_run_id"],
            content_preview=content_preview,
            event_type="graph",
            details={
                "graph_id": graph_trace["graph_id"],
                "run_id": graph_trace["node_run_id"],
                "node_id": graph_trace["node_id"],
                "parent_run_id": graph_trace["graph_run_id"],
                "node": {
                    "node_id": graph_trace["node_id"],
                    "skill_name": graph_trace["skill_name"],
                    "status": node_status,
                    "error": error_text or "",
                },
            },
        )
        record_trace_event(
            event_name="graph.run.done" if success else "graph.run.failed",
            phase="end",
            status=final_status,
            component="graph_executor",
            source="agent_loop",
            account_id=trace_identity["account_id"],
            tenant_id=trace_identity["tenant_id"],
            bot_id=trace_identity["bot_id"],
            channel=trace_identity["channel"],
            chat_id=trace_identity["chat_id"],
            client_id=trace_identity["client_id"],
            content_preview=content_preview,
            event_type="graph",
            details={
                "graph_id": graph_trace["graph_id"],
                "run_id": graph_trace["graph_run_id"],
                "graph": {
                    "graph_id": graph_trace["graph_id"],
                    "run_id": graph_trace["graph_run_id"],
                    "status": graph_status,
                    "node_count": 1,
                    "completed_nodes": completed_nodes,
                    "failed_nodes": failed_nodes,
                    "waiting_nodes": 0,
                    "running_nodes": 0,
                    "pending_nodes": 0,
                    "skipped_nodes": 0,
                    "current_node_ids": current_node_ids,
                    "topology": {
                        "nodes": [
                            {
                                "node_id": graph_trace["node_id"],
                                "skill_name": graph_trace["skill_name"],
                                "status": node_status,
                            }
                        ],
                        "edges": [],
                    },
                },
                "error": error_text or "",
            },
        )

    async def _execute_direct_tool_with_trace(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        route_metadata: dict[str, Any] | None,
        on_progress: Callable[[str], Awaitable[None]] | None,
        fallback_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[Any | None, str | None]:
        tool_call_id = f"direct_{tool_name}_{uuid.uuid4().hex}"
        active_bot_id = self._resolve_active_bot_id(route_metadata)
        guardrails_policy = self._resolve_bot_guardrails_policy(route_metadata)[1]
        self._trace_tool_lifecycle_start(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
            route_metadata=route_metadata,
            active_bot_id=active_bot_id,
            direct_invoke=True,
        )
        progress_handler = on_progress or fallback_progress
        if not self._guardrails_is_tool_allowed(
            tool_name,
            active_bot_id,
            guardrails_policy,
            route_metadata,
        ):
            blocked_result = ToolResult(
                content=f"错误：工具“{tool_name}”因安全策略限制，暂不可用。",
                success=False,
                error="guardrails_blocked",
            )
            self._trace_tool_blocked(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                route_metadata=route_metadata,
                active_bot_id=active_bot_id,
                content_preview=self._safe_json_dumps(tool_args),
                extra_details={"mode": "direct_plan"},
            )
            return blocked_result, blocked_result.content
        try:
            tool_skill_context = self._build_skill_context(
                route_metadata,
                progress_handler,
                active_bot_id,
                parent_run_id=tool_call_id,
            )
            tool_raw_result = await self._invoke_tool_via_plan(
                tool_name=tool_name,
                tool_args=tool_args,
                skill_context=tool_skill_context,
                route_metadata=route_metadata,
                on_progress=progress_handler,
                active_bot_id=active_bot_id,
                record_assistant_tool_call=False,
                enforce_guardrails=False,
            )
        except Exception as exc:
            error_text = str(exc)
            self._trace_tool_lifecycle_end(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                route_metadata=route_metadata,
                active_bot_id=active_bot_id,
                success=False,
            skill_content_preview=error_text,
            tool_content_preview=error_text,
                error_text=error_text,
                direct_invoke=True,
            )
            return None, error_text
        self._trace_tool_lifecycle_end(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            route_metadata=route_metadata,
            active_bot_id=active_bot_id,
            success=tool_raw_result.success,
            skill_content_preview=tool_raw_result.content,
            tool_content_preview=tool_raw_result.content,
            error_text=tool_raw_result.error,
            result_payload=tool_raw_result,
            direct_invoke=True,
        )
        return tool_raw_result, None

    def _resolve_active_bot_id(self, route_metadata: dict[str, Any] | None) -> str:
        if isinstance(route_metadata, dict):
            raw = route_metadata.get("bot_id")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        if isinstance(self.bot_config, dict):
            fallback = self.bot_config.get("id")
            if isinstance(fallback, str) and fallback.strip():
                return fallback.strip()
        return ""

    def _resolve_enabled_skills(self, bot_id: str | None) -> list[str]:
        if bot_id and bot_id in self.skills_config.bots:
            policy = self.skills_config.bots[bot_id]
            if policy.enabled_skills:
                return list(policy.enabled_skills)
        return list(self.skills_config.enabled_skills)

    def _record_routing_metric(self, tier: str, key: str) -> None:
        tier_key = tier or "unknown"
        bucket = self._routing_metrics.setdefault(tier_key, {})
        bucket[key] = bucket.get(key, 0) + 1

    @staticmethod
    def _normalize_routing_state(routing: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(routing or {})
        requested_purpose = str(
            payload.get("requested_purpose")
            or payload.get("purpose")
            or ""
        ).strip()
        selected_purpose = str(
            payload.get("selected_purpose")
            or payload.get("purpose")
            or requested_purpose
        ).strip()
        current_purpose = str(
            payload.get("current_purpose")
            or selected_purpose
            or requested_purpose
        ).strip()
        initial_model = str(
            payload.get("initial_model")
            or payload.get("model")
            or payload.get("final_model")
            or ""
        ).strip()
        current_model = str(
            payload.get("current_model")
            or payload.get("final_model")
            or initial_model
        ).strip()
        final_model = str(
            payload.get("final_model")
            or current_model
            or initial_model
        ).strip()
        initial_tier = payload.get("initial_tier") or payload.get("tier")
        current_tier = payload.get("current_tier") or initial_tier
        payload.update(
            {
                "requested_purpose": requested_purpose,
                "selected_purpose": selected_purpose,
                "current_purpose": current_purpose,
                "purpose": selected_purpose,
                "initial_model": initial_model,
                "current_model": current_model,
                "final_model": final_model,
                "model": initial_model,
                "initial_tier": initial_tier,
                "current_tier": current_tier,
                "tier": initial_tier,
                "fallback_count": int(payload.get("fallback_count") or 0),
                "fallback_used": bool(payload.get("fallback_used")),
                "success": bool(payload.get("success")),
            }
        )
        return payload

    @classmethod
    def _update_routing_state(
        cls,
        route_metadata: dict[str, Any] | None,
        *,
        requested_purpose: str | None = None,
        selected_purpose: str | None = None,
        current_purpose: str | None = None,
        initial_model: str | None = None,
        current_model: str | None = None,
        final_model: str | None = None,
        initial_tier: str | None = None,
        current_tier: str | None = None,
        success: bool | None = None,
        fallback_used: bool | None = None,
        fallback_count_delta: int = 0,
    ) -> dict[str, Any] | None:
        if not isinstance(route_metadata, dict):
            return None
        current = route_metadata.get("_routing")
        if not isinstance(current, dict):
            current = {}
        payload = cls._normalize_routing_state(current)
        if requested_purpose is not None:
            payload["requested_purpose"] = requested_purpose
        if selected_purpose is not None:
            payload["selected_purpose"] = selected_purpose
            payload["purpose"] = selected_purpose
        if current_purpose is not None:
            payload["current_purpose"] = current_purpose
        if initial_model is not None:
            payload["initial_model"] = initial_model
            payload["model"] = initial_model
        if current_model is not None:
            payload["current_model"] = current_model
        if final_model is not None:
            payload["final_model"] = final_model
            payload["current_model"] = final_model
        if initial_tier is not None:
            payload["initial_tier"] = initial_tier
            payload["tier"] = initial_tier
        if current_tier is not None:
            payload["current_tier"] = current_tier
        if success is not None:
            payload["success"] = success
        if fallback_used is not None:
            payload["fallback_used"] = fallback_used
        if fallback_count_delta:
            payload["fallback_count"] = int(payload.get("fallback_count") or 0) + fallback_count_delta
        route_metadata["_routing"] = cls._normalize_routing_state(payload)
        return route_metadata["_routing"]

    def _resolve_bot_guardrails_policy(
        self,
        route_metadata: dict[str, Any] | None,
    ) -> tuple[str, "BotGuardrailsPolicy | None"]:
        return self.policy.resolve_bot_guardrails_policy(route_metadata)

    def _record_bot_guardrails_hit(
        self,
        route_metadata: dict[str, Any] | None,
        bot_id: str,
        policy: str,
        action: str,
        detail: str,
    ) -> dict[str, Any]:
        meta = route_metadata if isinstance(route_metadata, dict) else {}
        entry = {
            "created_at_ms": int(time.time() * 1000),
            "bot_id": bot_id,
            "channel": str(meta.get("channel") or ""),
            "chat_id": str(meta.get("chat_id") or ""),
            "policy": policy,
            "action": action,
            "detail": detail,
        }
        self._bot_guardrails_history.append(entry)
        history_limit = max(10, int(self.bot_guardrails_config.history_limit))
        if len(self._bot_guardrails_history) > history_limit:
            del self._bot_guardrails_history[: len(self._bot_guardrails_history) - history_limit]
        if isinstance(route_metadata, dict):
            hits = route_metadata.setdefault("_bot_guardrails_turn_hits", [])
            if isinstance(hits, list):
                hits.append(entry)
            route_metadata["_bot_guardrails_last_hit"] = entry
        logger.warning(
            "Bot guardrails hit: bot_id={} policy={} action={} detail={}",
            bot_id,
            policy,
            action,
            detail,
        )
        return entry

    @staticmethod
    def _text_matches_pattern(text: str, pattern: str) -> bool:
        return AgentPolicy.text_matches_pattern(text, pattern)

    def _guardrails_block_instruction(
        self,
        text: str,
        bot_id: str,
        policy: "BotGuardrailsPolicy | None",
        route_metadata: dict[str, Any] | None,
    ) -> str | None:
        blocked_text, hits = self.policy.block_instruction(text, bot_id, policy)
        for hit in hits:
            self._record_bot_guardrails_hit(route_metadata, bot_id, hit.policy, hit.action, hit.detail)
        return blocked_text

    def _guardrails_review_output(
        self,
        text: str | None,
        bot_id: str,
        policy: "BotGuardrailsPolicy | None",
        route_metadata: dict[str, Any] | None,
    ) -> str | None:
        review = self.policy.review_output(text, bot_id, policy)
        for hit in review.guardrails_hits:
            self._record_bot_guardrails_hit(route_metadata, bot_id, hit.policy, hit.action, hit.detail)
        return review.text

    def _guardrails_is_tool_allowed(
        self,
        tool_name: str,
        bot_id: str,
        policy: "BotGuardrailsPolicy | None",
        route_metadata: dict[str, Any] | None,
    ) -> bool:
        is_allowed, hits = self.policy.is_tool_allowed(tool_name, bot_id, policy)
        for hit in hits:
            self._record_bot_guardrails_hit(route_metadata, bot_id, hit.policy, hit.action, hit.detail)
        return is_allowed

    @staticmethod
    def _repair_messages_for_retry(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system_msgs = [m for m in messages if m.get("role") == "system"]
        repaired_tail: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and msg.get("tool_calls"):
                continue
            content = msg.get("content")
            if isinstance(content, str) and content:
                repaired_tail.append({"role": role, "content": content})
        repaired_tail = repaired_tail[-12:]
        if system_msgs:
            return [system_msgs[0], *repaired_tail]
        return repaired_tail

    @staticmethod
    def _minimal_messages_for_retry(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system_msg = next((m for m in messages if m.get("role") == "system"), None)
        last_user = next(
            (
                m for m in reversed(messages)
                if m.get("role") == "user" and isinstance(m.get("content"), str) and m.get("content")
            ),
            None,
        )
        minimized: list[dict[str, Any]] = []
        if isinstance(system_msg, dict):
            content = system_msg.get("content")
            if isinstance(content, str) and content:
                minimized.append({"role": "system", "content": content})
        if isinstance(last_user, dict):
            minimized.append({"role": "user", "content": str(last_user["content"])})
        return minimized

    @staticmethod
    def _messages_signature(messages: list[dict[str, Any]]) -> str:
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _looks_like_interim_only(content: str | None) -> bool:
        text = (content or "").strip().lower()
        if not text:
            return False
        markers = (
            "以下为工具调用",
            "正在检查文件结构",
            "请稍等",
            "我将帮助您",
            "i will help you",
            "please wait",
            "tool call",
            "请稍候",
            "请稍后",
            "请稍候...",
            "请稍后...",
            "正在获取",
            "正在查询",
            "正在检索",
            "稍后给您",
        )
        return any(m in text for m in markers)

    @staticmethod
    def _describe_direct_handler(handler: Callable[..., Awaitable[Any]]) -> str:
        module_name = str(getattr(handler, "__module__", "") or "").strip()
        qualname = str(getattr(handler, "__qualname__", getattr(handler, "__name__", "handler")) or "handler").strip()
        return f"{module_name}.{qualname}" if module_name else qualname

    async def _handle_registered_direct_routes(
        self,
        *,
        msg: InboundMessage,
        history: list[dict[str, Any]],
        session: Session,
        sessions: SessionManager,
        memory_consolidator: MemoryConsolidator,
        interaction_context: dict[str, Any] | None,
        on_progress: Callable[..., Awaitable[None]] | None,
        fallback_progress: Callable[..., Awaitable[None]] | None,
        build_clean_outbound_metadata: Callable[[], dict[str, Any]],
    ) -> OutboundMessage | None:
        msg_metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        request_msg_id = str(
            msg_metadata.get("request_msg_id")
            or msg_metadata.get("message_id")
            or msg_metadata.get("parent_msg_id")
            or ""
        ).strip() or None
        for tool_name in self.tools.direct_route_names:
            handler = self.tools.get_direct_handler(tool_name)
            if handler is None:
                continue
            handler_source = self._describe_direct_handler(handler)
            started_at = time.perf_counter()
            record_trace_event(
                event_name="agent.direct_route",
                phase="running",
                status="running",
                component=tool_name,
                source="skill",
                account_id=msg.account_id or msg.sender_id,
                tenant_id=msg.tenant_id,
                bot_id=msg.bot_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                client_id=msg_metadata.get("client_id") if isinstance(msg_metadata.get("client_id"), str) else None,
                request_msg_id=request_msg_id,
                content_preview=msg.content,
                event_type="skill.direct_route",
                details={
                    "tool_name": tool_name,
                    "handler_source": handler_source,
                    "status_details": {
                        "execution": {
                            "mode": "direct_route",
                            "handler_source": handler_source,
                            "tool_name": tool_name,
                        }
                    },
                },
            )
            try:
                result = await handler(
                    loop=self,
                    msg=msg,
                    history=history,
                    session=session,
                    sessions=sessions,
                    memory_consolidator=memory_consolidator,
                    interaction_context=interaction_context,
                    on_progress=on_progress,
                    fallback_progress=fallback_progress,
                    build_clean_outbound_metadata=build_clean_outbound_metadata,
                )
            except Exception as exc:
                latency_ms = max(0, int((time.perf_counter() - started_at) * 1000))
                record_trace_event(
                    event_name="agent.direct_route",
                    phase="error",
                    status="error",
                    component=tool_name,
                    source="skill",
                    duration_ms=latency_ms,
                    account_id=msg.account_id or msg.sender_id,
                    tenant_id=msg.tenant_id,
                    bot_id=msg.bot_id,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    client_id=msg_metadata.get("client_id") if isinstance(msg_metadata.get("client_id"), str) else None,
                    request_msg_id=request_msg_id,
                    content_preview=msg.content,
                    event_type="skill.direct_route",
                    details={
                        "tool_name": tool_name,
                        "handler_source": handler_source,
                        "latency_ms": latency_ms,
                        "error": str(exc),
                        "status_details": {
                            "execution": {
                                "mode": "direct_route",
                                "handler_source": handler_source,
                                "tool_name": tool_name,
                                "latency_ms": latency_ms,
                            }
                        },
                    },
                )
                raise
            if result is not None:
                latency_ms = max(0, int((time.perf_counter() - started_at) * 1000))
                record_trace_event(
                    event_name="agent.direct_route",
                    phase="end",
                    status="ok",
                    component=tool_name,
                    source="skill",
                    duration_ms=latency_ms,
                    account_id=msg.account_id or msg.sender_id,
                    tenant_id=msg.tenant_id,
                    bot_id=msg.bot_id,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    client_id=msg_metadata.get("client_id") if isinstance(msg_metadata.get("client_id"), str) else None,
                    request_msg_id=request_msg_id,
                    content_preview=msg.content,
                    event_type="skill.direct_route",
                    details={
                        "tool_name": tool_name,
                        "handler_source": handler_source,
                        "latency_ms": latency_ms,
                        "handled": True,
                        "status_details": {
                            "execution": {
                                "mode": "direct_route",
                                "handler_source": handler_source,
                                "tool_name": tool_name,
                                "latency_ms": latency_ms,
                            }
                        },
                    },
                )
                return result
        return None

    @staticmethod
    def _normalize_progress_text(content: str | None) -> str | None:
        text = (content or "").strip()
        if not text:
            return None
        if len(text) > 220:
            return "正在执行工具并汇总结果"
        if text.count("\n") > 5:
            return "正在执行工具并汇总结果"
        if "```" in text or "\n## " in text or text.startswith("# "):
            return "正在执行工具并汇总结果"
        return text

    @staticmethod
    def _parse_tool_event_payload(result: ToolResult | str) -> tuple[str, dict[str, Any] | None]:
        result_text = result.content if isinstance(result, ToolResult) else result
        try:
            payload = json.loads(result_text)
        except Exception:
            return result_text, None
        if not isinstance(payload, dict):
            return result_text, None
        event_name = payload.get("_ithqbot_event")
        if not isinstance(event_name, str) or not event_name:
            if not any(key in payload for key in ("files", "interaction", "llm_result", "file_message")):
                return result_text, None
        llm_result = payload.get("llm_result")
        if not isinstance(llm_result, str) or not llm_result.strip():
            llm_result = "工具调用已处理完成。"
        return llm_result, payload

    async def _normalize_payload_files(
        self,
        payload: dict[str, Any] | None,
        *,
        skill_context: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return payload
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            return payload
        normalized: list[dict[str, Any]] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("file_id") or "").strip()
            if not file_id:
                file_id = str(
                    await self.file_service.resolve_file_id_from_legacy(
                        file_entry=item,
                        tenant_id=str(getattr(skill_context, "tenant_id", "") or ""),
                        account_id=str(getattr(skill_context, "account_id", "") or ""),
                        bot_id=str(getattr(skill_context, "bot_id", "") or ""),
                    )
                    or ""
                ).strip()
            if file_id:
                normalized.append({"file_id": file_id})
        if normalized:
            payload["files"] = normalized
            payload.setdefault(
                "file_message",
                "结果文件已生成，可通过 file_id 继续读取或处理。",
            )
        return payload

    async def _build_outbound_file_entries(
        self,
        files: list[dict[str, Any]] | None,
        *,
        tenant_id: str,
        account_id: str,
        bot_id: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(files, list) or not files:
            return []
        normalized: list[dict[str, Any]] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("file_id") or "").strip()
            if not file_id:
                storage = item.get("storage")
                rel_path = str(item.get("rel_path") or "").strip()
                if isinstance(storage, dict) or rel_path:
                    normalized.append(item)
                continue
            try:
                meta = await self.file_service.get(
                    file_id=file_id,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    bot_id=bot_id,
                    include_internal=True,
                )
            except Exception:
                fallback_entry = {"file_id": file_id}
                if item.get("download_url"):
                    fallback_entry["download_url"] = item.get("download_url")
                if item.get("name"):
                    fallback_entry["name"] = item.get("name")
                if item.get("mime"):
                    fallback_entry["mime"] = item.get("mime")
                if item.get("size"):
                    fallback_entry["size"] = item.get("size")
                if item.get("storage"):
                    fallback_entry["storage"] = item.get("storage")
                normalized.append(fallback_entry)
                continue
            storage = meta.get("storage") if isinstance(meta.get("storage"), dict) else {}
            object_key = str(storage.get("object_key") or "").strip()
            bucket = str(storage.get("bucket") or getattr(self.file_service, "_bucket", "") or "").strip()
            entry: dict[str, Any] = {
                "file_id": file_id,
                "name": meta.get("name"),
                "mime": meta.get("mime"),
                "size": meta.get("size"),
                "download_url": meta.get("download_url"),
            }
            if object_key:
                entry["rel_path"] = object_key
                entry["storage"] = {
                    "backend": "minio",
                    "bucket": bucket,
                    "path": object_key,
                }
            normalized.append(entry)
        return normalized

    @staticmethod
    def _tool_call_signature(tool_call: ToolCallRequest) -> str:
        try:
            normalized_args = json.dumps(tool_call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            normalized_args = str(tool_call.arguments)
        return f"{tool_call.name}:{normalized_args}"

    @staticmethod
    def _duplicate_tool_call_result(tool_name: str) -> str:
        return (
            f"错误：检测到重复工具调用“{tool_name}”，相同参数的调用刚刚已经执行过。"
            "请不要继续重复调用工具，直接基于已有结果给出最终答复。"
        )

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        candidate = (text or "").strip()
        if not candidate:
            return None
        fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", candidate, re.IGNORECASE)
        if fenced_match:
            candidate = fenced_match.group(1).strip()
        for raw in (candidate, candidate[candidate.find("{"): candidate.rfind("}") + 1] if "{" in candidate and "}" in candidate else ""):
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _recover_textual_tool_calls(self, content: str | None) -> tuple[list[ToolCallRequest], str | None]:
        raw = (content or "").strip()
        if not raw:
            return [], None
        pattern = re.compile(
            r"<function(?:\s+name=['\"](?P<attr_name>[^'\"]+)['\"])?\s*>\s*(?:(?P<inline_name>[A-Za-z0-9_.:-]+)\s*)?(?P<body>[\s\S]*?)</function>",
            re.IGNORECASE,
        )
        calls: list[ToolCallRequest] = []
        stripped = raw
        for idx, match in enumerate(pattern.finditer(raw), start=1):
            tool_name = (match.group("attr_name") or match.group("inline_name") or "").strip()
            if not tool_name or not self.tools.has(tool_name):
                continue
            args = self._extract_json_object(match.group("body") or "")
            if args is None:
                continue
            calls.append(
                ToolCallRequest(
                    id=f"call_recovered_{int(time.time() * 1000)}_{idx}",
                    name=tool_name,
                    arguments=args,
                )
            )
            stripped = stripped.replace(match.group(0), "").strip()
        if not calls:
            return [], content
        return calls, stripped or None

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        route_text: str = "",
        route_metadata: dict[str, Any] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        session_id = str((route_metadata or {}).get("session_key") or "")
        trace_id = str((route_metadata or {}).get("trace_id") or (route_metadata or {}).get("request_msg_id") or "").strip() or None
        run_id = str((route_metadata or {}).get("run_id") or (route_metadata or {}).get("request_msg_id") or uuid.uuid4()).strip()
        runtime_source = self._runtime_controls.setdefault(
            session_id or run_id,
            CancellationTokenSource(metadata={"session_id": session_id or run_id}),
        )
        policy_turn = await self.policy.resolve_turn(
            route_text=route_text,
            route_metadata=route_metadata,
            tool_defs=self.tools.get_definitions(),
        )
        active_bot_id = policy_turn.active_bot_id
        guardrails_policy = policy_turn.guardrails_policy
        if isinstance(route_metadata, dict):
            route_metadata["_bot_guardrails_turn_hits"] = []
        for hit in policy_turn.guardrails_hits:
            self._record_bot_guardrails_hit(route_metadata, active_bot_id, hit.policy, hit.action, hit.detail)
        if isinstance(route_metadata, dict):
            route_metadata["_bot_guardrails_allowed_tools"] = list(policy_turn.allowed_tool_names)
            route_metadata["_decision_trace"] = list(policy_turn.decision_trace)
            if policy_turn.routing_state:
                route_metadata["_routing"] = self._normalize_routing_state(policy_turn.routing_state)
        if policy_turn.decision is not None:
            record_trace_event(
                event_name="agent.route.decided",
                phase="point",
                status="ok",
                component="agent_loop",
                source="agent_loop",
                account_id=(route_metadata or {}).get("account_id") if isinstance(route_metadata, dict) else None,
                tenant_id=(route_metadata or {}).get("tenant_id") if isinstance(route_metadata, dict) else None,
                bot_id=active_bot_id,
                channel=(route_metadata or {}).get("channel") if isinstance(route_metadata, dict) else None,
                chat_id=(route_metadata or {}).get("chat_id") if isinstance(route_metadata, dict) else None,
                client_id=(route_metadata or {}).get("client_id") if isinstance(route_metadata, dict) else None,
                content_preview=route_text,
                event_type="routing",
                details={
                    "routing": build_routing_observability_payload(
                        dict((route_metadata or {}).get("_routing"))
                        if isinstance(route_metadata, dict)
                        and isinstance(route_metadata.get("_routing"), dict)
                        else policy_turn.routing_state,
                    ),
                    **(
                        dict((route_metadata or {}).get("_routing"))
                        if isinstance(route_metadata, dict)
                        and isinstance(route_metadata.get("_routing"), dict)
                        else {
                            "source": policy_turn.decision.source,
                            "tier": policy_turn.decision.tier,
                            "model": policy_turn.decision.model,
                            "fallback_models": list(policy_turn.decision.fallback_models),
                            "max_tokens": policy_turn.decision.max_tokens,
                            "reasoning_effort": policy_turn.decision.reasoning_effort,
                        }
                    ),
                },
            )
            self._record_routing_metric(policy_turn.decision.tier, "requests")
            logger.info(
                "Model route: source={}, tier={}, model={}, fallbacks={}",
                policy_turn.decision.source,
                policy_turn.decision.tier,
                policy_turn.active_model,
                policy_turn.fallback_models,
            )
        skill_context = self._build_skill_context(
            route_metadata,
            on_progress,
            active_bot_id,
            cancellation_token=runtime_source.token,
        )
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        checkpoint = RuntimeCheckpoint.create(
            session_id=session_id or run_id,
            run_id=run_id,
            trace_id=trace_id,
            route_metadata=dict(route_metadata or {}),
            status=RuntimeStatus.RUNNING,
        )
        checkpoint = self._save_runtime_checkpoint(checkpoint) or checkpoint
        if policy_turn.blocked_response is not None:
            if isinstance(route_metadata, dict):
                route_metadata["_bot_guardrails_blocked"] = True
            checkpoint = self._save_runtime_checkpoint(
                checkpoint.with_status(RuntimeStatus.COMPLETED),
                expected_version=checkpoint.version,
            ) or checkpoint
            return policy_turn.blocked_response, tools_used, messages

        decision = policy_turn.decision
        active_model = policy_turn.active_model
        fallback_models = list(policy_turn.fallback_models or [])
        guarded_tool_defs = list(policy_turn.guarded_tool_defs)
        planner_state = PlannerIterationState()
        executor_state = ExecutorState()

        while iteration < self.max_iterations:
            runtime_source.token.throw_if_cancelled()
            iteration += 1
            planner_result = await self.planner.plan_iteration(
                messages=messages,
                guarded_tool_defs=guarded_tool_defs,
                active_model=active_model,
                route_text=route_text,
                route_metadata=route_metadata,
                iteration=iteration,
                decision=decision,
                fallback_models=fallback_models,
                state=planner_state,
            )
            messages = planner_result.messages
            active_model = planner_result.active_model or active_model
            if planner_result.should_continue:
                continue
            if planner_result.final_content is not None and planner_result.response is None:
                final_content = planner_result.final_content
                break

            response = planner_result.response
            if response is None:
                break

            if not planner_result.plan.is_empty:
                execution_result = await self.executor.execute_plan(
                    plan=planner_result.plan,
                    response_content=response.content,
                    response_reasoning_content=response.reasoning_content,
                    response_thinking_blocks=response.thinking_blocks,
                    messages=messages,
                    skill_context=skill_context,
                    on_progress=on_progress,
                    route_metadata=route_metadata,
                    active_bot_id=active_bot_id,
                    guardrails_policy=guardrails_policy,
                    iteration=iteration,
                    state=executor_state,
                )
                runtime_inputs = {
                    "route_text": route_text,
                    "iteration": iteration,
                    "active_model": active_model,
                    "active_bot_id": active_bot_id,
                    "decision_trace": list(policy_turn.decision_trace),
                }
                plan_record = planner_result.plan.to_record(
                    inputs=runtime_inputs,
                    outputs=execution_result.to_dict(),
                )
                if isinstance(route_metadata, dict):
                    route_metadata["_plan_record"] = plan_record
                messages = execution_result.messages
                tools_used.extend(execution_result.tools_used)
                checkpoint = self._save_runtime_checkpoint(
                    RuntimeCheckpoint.from_dict(
                        {
                            **checkpoint.to_dict(),
                            "status": RuntimeStatus.RUNNING.value,
                            "plan": planner_result.plan.to_dict(),
                            "cursor": RuntimeCursor(
                                phase="executor",
                                iteration=iteration,
                                node_id=execution_result.pending_node_id,
                                tool_call_id=execution_result.pending_node_id,
                            ).to_dict(),
                            "tool_outputs": {
                                **checkpoint.tool_outputs,
                                **{
                                    node_id: result.to_dict()
                                    for node_id, result in execution_result.node_results.items()
                                },
                            },
                            "messages_snapshot": self._checkpoint_messages(messages),
                            "cancellation": runtime_source.snapshot().to_dict(),
                        }
                    ),
                    expected_version=checkpoint.version,
                ) or checkpoint
                if execution_result.stop_loop:
                    final_content = execution_result.final_content
                    if execution_result.pending_interaction:
                        if isinstance(route_metadata, dict):
                            route_metadata["_pending_interaction"] = dict(execution_result.pending_interaction)
                        interaction_request = self._normalize_interaction_request(
                            execution_result.pending_interaction,
                            session_id=checkpoint.session_id,
                        )
                        if interaction_request is not None:
                            checkpoint = self._save_runtime_checkpoint(
                                RuntimeCheckpoint.from_dict(
                                    {
                                        **checkpoint.to_dict(),
                                        "status": RuntimeStatus.WAITING_HUMAN.value,
                                        "cursor": RuntimeCursor(
                                            phase="human",
                                            iteration=iteration,
                                            node_id=execution_result.pending_node_id,
                                            tool_call_id=execution_result.pending_node_id,
                                        ).to_dict(),
                                        "pending_interaction": interaction_request.to_dict(),
                                        "messages_snapshot": self._checkpoint_messages(messages),
                                        "cancellation": runtime_source.snapshot().to_dict(),
                                    }
                                ),
                                expected_version=checkpoint.version,
                            ) or checkpoint
                    self._record_routing_metric(decision.tier, 'tool_loop_blocked')
                    self._update_routing_state(
                        route_metadata,
                        current_model=active_model,
                        final_model=active_model,
                        success=False,
                    )
                    break
                continue

            clean = self._strip_think(response.content)
            if response.finish_reason == 'error':
                logger.error('LLM returned error: {}', (clean or '')[:200])
                final_content = clean or '调用模型时出现异常，请稍后重试。'
                self._record_routing_metric(decision.tier, 'errors')
                self._update_routing_state(
                    route_metadata,
                    current_model=active_model,
                    final_model=active_model,
                    success=False,
                )
                checkpoint = self._save_runtime_checkpoint(
                    checkpoint.with_status(RuntimeStatus.FAILED),
                    expected_version=checkpoint.version,
                ) or checkpoint
                break
            if self._looks_like_interim_only(clean) and iteration < self.max_iterations:
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append({
                    'role': 'user',
                    'content': '请继续直接执行并输出结果；不要只描述计划，也不要输出“以下为工具调用”这类占位文本。',
                })
                logger.info('Assistant returned interim-only text without tool calls; forcing continuation')
                continue
            messages = self.context.add_assistant_message(
                messages,
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
            final_content = clean
            self._update_routing_state(
                route_metadata,
                current_model=active_model,
                final_model=active_model,
                success=True,
            )
            checkpoint = self._save_runtime_checkpoint(
                RuntimeCheckpoint.from_dict(
                    {
                        **checkpoint.with_status(RuntimeStatus.COMPLETED).to_dict(),
                        "messages_snapshot": self._checkpoint_messages(messages),
                        "cancellation": runtime_source.snapshot().to_dict(),
                    }
                ),
                expected_version=checkpoint.version,
            ) or checkpoint
            record_trace_event(
                event_name='agent.reply.generated',
                phase='end',
                status='ok',
                component='agent_loop',
                source='agent_loop',
                account_id=(route_metadata or {}).get('account_id') if isinstance(route_metadata, dict) else None,
                tenant_id=(route_metadata or {}).get('tenant_id') if isinstance(route_metadata, dict) else None,
                bot_id=active_bot_id,
                channel=(route_metadata or {}).get('channel') if isinstance(route_metadata, dict) else None,
                chat_id=(route_metadata or {}).get('chat_id') if isinstance(route_metadata, dict) else None,
                client_id=(route_metadata or {}).get('client_id') if isinstance(route_metadata, dict) else None,
                content_preview=clean,
                event_type='message.reply',
                details={
                    'iteration': iteration,
                    'model': active_model,
                    'tools_used': list(tools_used),
                    'routing': build_routing_observability_payload(
                        (route_metadata or {}).get('_routing') if isinstance(route_metadata, dict) else None,
                    ),
                },
            )
            self._record_routing_metric(decision.tier, 'success')
            break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning('Max iterations ({}) reached', self.max_iterations)
            final_content = '我尝试查询但未拿到可用结果，请稍后重试，或提供更具体的查询条件。'
            messages = self.context.add_assistant_message(messages, final_content)
            self._record_routing_metric(decision.tier, 'max_iterations')
            self._update_routing_state(
                route_metadata,
                current_model=active_model,
                final_model=active_model,
                success=False,
            )
            checkpoint = self._save_runtime_checkpoint(
                RuntimeCheckpoint.from_dict(
                    {
                        **checkpoint.with_status(RuntimeStatus.FAILED).to_dict(),
                        "messages_snapshot": self._checkpoint_messages(messages),
                        "cancellation": runtime_source.snapshot().to_dict(),
                    }
                ),
                expected_version=checkpoint.version,
            ) or checkpoint

        final_content = self._guardrails_review_output(
            final_content,
            active_bot_id,
            guardrails_policy,
            route_metadata,
        )
        return final_content, tools_used, messages

    def _checkpoint_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        for message in messages[-40:]:
            entry = dict(message)
            content = entry.get("content")
            if isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            compacted.append(entry)
        return compacted

    def _load_runtime_checkpoint(self, session_id: str) -> RuntimeCheckpoint | None:
        if not session_id:
            return None
        try:
            return self._runtime_checkpoint_store.load(session_id)
        except Exception:
            logger.exception("Failed to load runtime checkpoint for {}", session_id)
            return None

    def _save_runtime_checkpoint(
        self,
        checkpoint: RuntimeCheckpoint | None,
        *,
        expected_version: int | None = None,
    ) -> RuntimeCheckpoint | None:
        if checkpoint is None:
            return None
        try:
            return self._runtime_checkpoint_store.save(checkpoint, expected_version=expected_version)
        except Exception:
            logger.exception("Failed to save runtime checkpoint for {}", checkpoint.session_id)
            return checkpoint

    @staticmethod
    def _normalize_interaction_request(payload: dict[str, Any], *, session_id: str) -> HumanInteractionRequest | None:
        normalized = dict(payload)
        normalized.setdefault("session_id", session_id)
        normalized.setdefault("prompt", str(payload.get("prompt") or payload.get("title") or "").strip())
        if "schema" not in normalized:
            normalized["schema"] = {
                "fields": list(payload.get("fields") or []),
                "options": list(payload.get("options") or []),
            }
        return HumanInteractionRequest.from_dict(normalized)

    @staticmethod
    def _coerce_interaction_response(
        payload: dict[str, Any] | None,
        *,
        session_id: str,
    ) -> HumanInteractionResponse | None:
        if not isinstance(payload, dict):
            return None
        normalized = dict(payload)
        normalized.setdefault("session_id", session_id)
        normalized.setdefault("interaction_id", str(payload.get("interaction_id") or "").strip())
        return HumanInteractionResponse.from_dict(normalized)

    @staticmethod
    def _build_interaction_resume_message(
        content: str,
        interaction_response: HumanInteractionResponse,
    ) -> str:
        return (
            f"继续上一次等待中的交互（interaction_id={interaction_response.interaction_id}）。\n"
            f"用户补录内容：{json.dumps(interaction_response.data, ensure_ascii=False)}\n"
            f"附加消息：{content or ''}"
        ).strip()

    @staticmethod
    def _safe_json_dumps(payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)

    @staticmethod
    async def _emit_progress(
        on_progress: Callable[..., Awaitable[None]] | None,
        content: str,
        *,
        tool_hint: bool = False,
        progress_percent: int | None = None,
        progress_kind: str | None = None,
        progress_stage: str | None = None,
        status_event: str = "processing",
        interaction: dict[str, Any] | None = None,
        files: list[dict[str, Any]] | None = None,
        content_type: str | None = None,
        tool_name: str | None = None,
        skill_name: str | None = None,
        call_type: str | None = None,
        status_details: dict[str, Any] | None = None,
    ) -> None:
        if not on_progress:
            return
        kwargs: dict[str, Any] = {}
        if tool_hint:
            kwargs["tool_hint"] = True
        if progress_percent is not None:
            kwargs["progress_percent"] = progress_percent
        if progress_kind:
            kwargs["progress_kind"] = progress_kind
        if progress_stage:
            kwargs["progress_stage"] = progress_stage
        if status_event:
            kwargs["status_event"] = status_event
        if isinstance(interaction, dict):
            kwargs["interaction"] = interaction
        if isinstance(files, list):
            kwargs["files"] = files
        if content_type:
            kwargs["content_type"] = content_type
        if tool_name:
            kwargs["tool_name"] = tool_name
        if skill_name:
            kwargs["skill_name"] = skill_name
        if call_type:
            kwargs["call_type"] = call_type
        if isinstance(status_details, dict):
            kwargs["status_details"] = status_details
        try:
            await on_progress(content, **kwargs)
            return
        except TypeError:
            pass
        if tool_hint:
            try:
                await on_progress(content, tool_hint=True)
                return
            except TypeError:
                pass
        await on_progress(content)

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await self._consume_next_runtime_message()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            cmd = msg.content.strip().lower()
            if cmd == "/stop":
                await self._handle_stop(msg)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._track_active_task(msg.session_key, task)

    async def _consume_next_runtime_message(self) -> InboundMessage:
        runtime_execution_get = asyncio.create_task(self.bus.consume_runtime_execution())
        runtime_ingress_get = asyncio.create_task(self.bus.consume_runtime_ingress())
        inbound_get = asyncio.create_task(self.bus.consume_inbound())
        done, pending = await asyncio.wait(
            {runtime_execution_get, runtime_ingress_get, inbound_get},
            timeout=1.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            for task in pending:
                task.cancel()
            raise asyncio.TimeoutError()

        try:
            completed = done.pop()
            payload = await completed
            if hasattr(payload, "payload") and isinstance(payload.payload, InboundMessage):
                return payload.payload
            if isinstance(payload, InboundMessage):
                return payload
            raise TypeError(f"unsupported runtime payload: {type(payload).__name__}")
        finally:
            for task in pending:
                task.cancel()

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks for the session."""
        token_source = self._runtime_controls.get(msg.session_key)
        cooperative_cancelled = bool(token_source and token_source.cancel("user_cancelled", {"requested_by": "user"}))
        tasks = self._active_tasks.get(msg.session_key, [])
        done, pending = await asyncio.wait(tasks, timeout=1.5) if tasks else (set(), set())
        cancelled = sum(1 for t in pending if not t.done() and t.cancel())
        self._active_tasks.pop(msg.session_key, None)
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        total = len(done) + cancelled if cooperative_cancelled else cancelled
        content = (
            f"Stopped {total} task(s)."
            if total
            else ("Cancellation requested." if cooperative_cancelled else "No active task to stop.")
        )
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        ))

        async def _do_restart():
            await asyncio.sleep(1)
            # Use -m ithqbot instead of sys.argv[0] for Windows compatibility
            # (sys.argv[0] may be just "ithqbot" without full path on Windows)
            os.execv(sys.executable, [sys.executable, "-m", "ithqbot"] + sys.argv[1:])

        self._schedule_background(_do_restart())

    def _track_active_task(self, session_key: str, task: asyncio.Task[Any]) -> None:
        self._active_tasks.setdefault(session_key, []).append(task)
        task.add_done_callback(lambda done, key=session_key: self._untrack_active_task(key, done))

    def _untrack_active_task(self, session_key: str, task: asyncio.Task[Any]) -> None:
        tasks = self._active_tasks.get(session_key)
        if tasks and task in tasks:
            tasks.remove(task)
            if not tasks:
                self._active_tasks.pop(session_key, None)
        self._cleanup_session_lock(session_key)

    def _cleanup_session_lock(self, session_key: str) -> None:
        """Drop local session bookkeeping once the worker and queue are both gone."""
        if session_key in self._session_queues:
            return
        if self._active_tasks.get(session_key):
            return
        self._session_locks.pop(session_key, None)
        self._runtime_controls.pop(session_key, None)

    async def _session_worker(self, session_key: str, queue: asyncio.Queue) -> None:
        """Actor loop for processing messages lock-free per session."""
        try:
            while True:
                msg, future = await queue.get()
                
                msg_id = str(msg.metadata.get("request_msg_id") or msg.metadata.get("message_id") or "")
                trace_id = str(msg.metadata.get("trace_id") or msg_id)
                
                with logger.contextualize(trace_id=trace_id, msg_id=msg_id):
                    try:
                        async with self.sessions.session_lock(session_key):
                            response = await self._process_message(msg)
                            
                        if response is not None:
                            await self.bus.publish_outbound(response)
                        elif msg.channel == "cli":
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="", metadata=msg.metadata or {},
                            ))
                            
                        if not future.done():
                            future.set_result(None)
                            
                    except TimeoutError:
                        logger.warning("Timed out waiting for distributed session lock {}", session_key)
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="当前会话仍在处理中，请稍后重试。",
                            account_id=msg.account_id or msg.sender_id,
                            tenant_id=msg.tenant_id,
                            bot_id=msg.bot_id,
                            metadata=msg.metadata or {},
                        ))
                        if not future.done():
                            future.set_result(None)
                            
                    except asyncio.CancelledError:
                        logger.info("Task cancelled for session {}", msg.session_key)
                        record_trace_event(
                            event_name="agent.task.cancelled",
                            phase="point",
                            status="cancelled",
                            component="agent_loop",
                            account_id=msg.account_id,
                            tenant_id=msg.tenant_id,
                            bot_id=msg.bot_id,
                            chat_id=msg.chat_id,
                            request_msg_id=msg_id,
                            trace_id=trace_id,
                        )
                        if not future.done():
                            future.set_exception(asyncio.CancelledError())
                        raise
                        
                    except Exception as e:
                        logger.exception("Error processing message for session {}", msg.session_key)
                        record_trace_event(
                            event_name="agent.task.error",
                            phase="point",
                            status="error",
                            component="agent_loop",
                            account_id=msg.account_id,
                            tenant_id=msg.tenant_id,
                            bot_id=msg.bot_id,
                            chat_id=msg.chat_id,
                            request_msg_id=msg_id,
                            trace_id=trace_id,
                            details={"error": str(e)},
                        )
                        route_metadata = dict(msg.metadata or {})
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="处理请求时出现异常，请稍后重试。",
                            account_id=msg.account_id or msg.sender_id,
                            tenant_id=msg.tenant_id,
                            bot_id=msg.bot_id,
                            metadata=route_metadata,
                        ))
                        if not future.done():
                            future.set_result(None)
                            
                queue.task_done()
        finally:
            self._session_queues.pop(session_key, None)
            while not queue.empty():
                _, future = queue.get_nowait()
                if not future.done():
                    future.set_exception(asyncio.CancelledError())

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Lock-free message dispatch using a per-session queue."""
        session_key = msg.session_key
        queue = self._session_queues.get(session_key)
        
        if queue is None:
            queue = asyncio.Queue()
            self._session_queues[session_key] = queue
            task = asyncio.create_task(self._session_worker(session_key, queue))
            self._track_active_task(session_key, task)

        self._session_locks[session_key] = True
        self._runtime_controls.setdefault(session_key, CancellationTokenSource(metadata={"session_id": session_key}))
        future = asyncio.Future()
        queue.put_nowait((msg, future))

        try:
            await future
        except asyncio.CancelledError:
            raise
        finally:
            if queue.empty():
                self._session_locks.pop(session_key, None)


    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._graph_state_manager is not None:
            close = getattr(self._graph_state_manager, "close", None)
            if callable(close):
                close()
            self._graph_state_manager = None
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        if task in self._background_tasks:
            self._background_tasks.remove(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.opt(exception=exc).error("Background task failed")

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        from ithqbot import context
        account = msg.account_id or msg.sender_id
        tenant = msg.tenant_id or (msg.metadata.get("tenant_id") if msg.metadata else None)
        bot = msg.bot_id or (msg.metadata.get("bot_id") if msg.metadata else None)
        effective_channel = msg.channel
        effective_chat_id = msg.chat_id
        if msg.channel == "system" and ":" in msg.chat_id:
            effective_channel, effective_chat_id = msg.chat_id.split(":", 1)
        tokens = context.set_runtime_context(
            account=account,
            tenant=tenant,
            bot=bot,
            channel_name=effective_channel,
            chat=effective_chat_id,
            metadata=msg.metadata,
        )
        try:
            return await self._process_message_internal(msg, session_key, on_progress)
        finally:
            context.reset_runtime_context(tokens)

    async def _process_message_internal(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Internal processing logic with context already set."""
        # 每回合重置 token 用量累计器；回合结束时把真实用量写进出站元信息
        reset_turn_usage()
        workspace = Path(msg.workspace_override) if msg.workspace_override else self.workspace
        active_bot_id = msg.bot_id or (msg.metadata.get("bot_id") if msg.metadata else None) or (self.bot_config.get("id") if isinstance(self.bot_config, dict) else None)
        enabled_skills = self._resolve_enabled_skills(active_bot_id)
        if msg.workspace_override:
            # Create local managers for this isolated workspace
            context = ContextBuilder(
                workspace,
                enabled_skills=enabled_skills,
                memory_store_uri=self.memory_store_uri,
                allow_local_memory_fallback=not self.require_external_memory_store,
            )
            from ithqbot.session.factory import create_session_store
            sessions = SessionManager(
                workspace=workspace,
                store=create_session_store(
                    workspace,
                    config_uri=self.session_store_uri,
                    require_external_store=self.require_external_session_store,
                ),
            )
            memory_consolidator = MemoryConsolidator(
                workspace=workspace,
                provider=self.provider,
                model=self.model,
                sessions=sessions,
                context_window_tokens=self.context_window_tokens,
                build_messages=context.build_messages,
                get_tool_definitions=self.tools.get_definitions,
                memory_store_uri=self.memory_store_uri,
                allow_local_memory_fallback=not self.require_external_memory_store,
            )
            self._set_tool_workspace(workspace)
        else:
            context = ContextBuilder(
                workspace,
                enabled_skills=enabled_skills,
                memory_store_uri=self.memory_store_uri,
                allow_local_memory_fallback=not self.require_external_memory_store,
            )
            sessions = self.sessions
            memory_consolidator = self.memory_consolidator
            self._set_tool_workspace(self.workspace)

        # Resolve bot configuration if present
        if self.bot_config:
            if msg.metadata is None:
                msg.metadata = {}
            msg.metadata.setdefault("bot_config", self.bot_config)

        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = sessions.get_or_create(key)
            await memory_consolidator.maybe_consolidate_by_tokens(session)
            history = session.get_history(max_messages=self._MAX_HISTORY_MESSAGES)
            messages = context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                metadata=msg.metadata
            )
            route_metadata = dict(msg.metadata or {})
            route_metadata.setdefault("channel", channel)
            route_metadata.setdefault("chat_id", chat_id)
            route_metadata.setdefault("tenant_id", msg.tenant_id)
            route_metadata.setdefault("account_id", msg.account_id or msg.sender_id)
            route_metadata.setdefault("bot_id", msg.bot_id or route_metadata.get("bot_id") or self._resolve_active_bot_id(route_metadata))
            final_content, _, all_msgs = await self._run_agent_loop(
                messages,
                route_text=msg.content,
                route_metadata=route_metadata,
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self._persist_session_state(sessions, memory_consolidator, session)
            self._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
            sys_meta = dict(msg.metadata or {})
            for key in ("_bot_guardrails_turn_hits", "_bot_guardrails_last_hit", "_bot_guardrails_blocked", "_routing"):
                if key in route_metadata:
                    sys_meta[key] = route_metadata[key]
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                   content=final_content or "Background task completed.",
                                   metadata=sys_meta)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)
        record_trace_event(
            event_name="agent.message.received",
            phase="point",
            status="ok",
            component="agent_loop",
            source=msg.channel,
            account_id=msg.account_id or msg.sender_id,
            tenant_id=msg.tenant_id,
            bot_id=msg.bot_id,
            channel=msg.channel,
            chat_id=msg.chat_id,
            client_id=(msg.metadata or {}).get("client_id") if isinstance(msg.metadata, dict) else None,
            content_preview=msg.content,
            event_type="message.user",
            details={
                "has_attachments": bool(msg.metadata and (msg.metadata.get("attachments") or msg.metadata.get("file_meta"))),
                "session_key": session_key or msg.session_key,
            },
        )

        key = session_key or msg.session_key
        session = sessions.get_or_create(key)

        # Check if this is a pure file upload notification to suppress progress UI
        is_file_upload = False
        if msg.metadata and (msg.metadata.get("attachments") or msg.metadata.get("file_meta")):
            # If content is empty or just a [file: ...] placeholder, it's a file upload
            content = msg.content or ""
            if not content.strip() or (content.startswith("[") and content.endswith("]") and "file" in content.lower()):
                is_file_upload = True

        def _build_clean_outbound_metadata() -> dict[str, Any]:
            meta = dict(msg.metadata or {})
            for key in (
                "interaction",
                "interaction_response",
                "attachments",
                "files",
                "file_meta",
                "content_type",
                "status_event",
                "_status_details",
                "_progress",
                "_tool_hint",
                "_progress_percent",
                "_progress_kind",
                "_progress_stage",
            ):
                meta.pop(key, None)
            return meta

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            progress_percent: int | None = None,
            progress_kind: str | None = None,
            progress_stage: str | None = None,
            status_event: str = "processing",
            interaction: dict[str, Any] | None = None,
            files: list[dict[str, Any]] | None = None,
            content_type: str | None = None,
            tool_name: str | None = None,
            skill_name: str | None = None,
            call_type: str | None = None,
            status_details: dict[str, Any] | None = None,
        ) -> None:
            meta = _build_clean_outbound_metadata()
            if status_event == "processing":
                meta["_progress"] = True
            else:
                meta.pop("_progress", None)
            meta["_tool_hint"] = tool_hint
            if progress_percent is not None:
                meta["_progress_percent"] = progress_percent
            if progress_kind:
                meta["_progress_kind"] = progress_kind
            if progress_stage:
                meta["_progress_stage"] = progress_stage
            if isinstance(interaction, dict):
                meta["interaction"] = interaction
            if isinstance(files, list):
                meta["files"] = files
            if content_type:
                meta["content_type"] = content_type
            if tool_name:
                meta["_tool_name"] = tool_name
            if skill_name:
                meta["_skill_name"] = skill_name
            if call_type:
                meta["_call_type"] = call_type
            if isinstance(status_details, dict):
                meta["_status_details"] = self._build_processing_status_details(
                    content,
                    progress_stage=progress_stage,
                    tool_name=tool_name,
                    skill_name=skill_name,
                    call_type=call_type,
                    status_details=status_details,
                )
            elif status_event == "processing":
                meta["_status_details"] = self._build_processing_status_details(
                    content,
                    progress_stage=progress_stage,
                    tool_name=tool_name,
                    skill_name=skill_name,
                    call_type=call_type,
                )
            meta["status_event"] = status_event
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        # 1. Queued phase
        if not is_file_upload:
            await self._emit_progress(
                on_progress or _bus_progress,
                "任务已进入队列",
                progress_percent=5,
                progress_kind="status",
                progress_stage="queued",
            )

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            snapshot = session.messages[session.last_consolidated:]
            session.clear()
            sessions.save(session)
            sessions.invalidate(session.key)

            if snapshot:
                self._schedule_background(memory_consolidator.archive_messages(snapshot))

            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                   content="New session started.")
        if cmd == "/help":
            lines = [
                "🐈 ithqbot commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/help — Show available commands",
                "/graph plan <request> — Use planner to build and execute a graph",
                "/graph preview <request> — Preview the generated graph JSON",
                "/graph run <graph_id> — Execute a graph from workspace graphs/",
            ]
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content="\n".join(lines),
            )
        await memory_consolidator.maybe_consolidate_by_tokens(session)

        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self._MAX_HISTORY_MESSAGES)
        interaction_response = (msg.metadata or {}).get("interaction_response") if isinstance(msg.metadata, dict) else None
        interaction_context = interaction_response.get("context") if isinstance(interaction_response, dict) else None
        route_metadata = dict(msg.metadata or {})
        route_metadata.setdefault("channel", msg.channel)
        route_metadata.setdefault("chat_id", msg.chat_id)
        route_metadata.setdefault("tenant_id", msg.tenant_id)
        route_metadata.setdefault("account_id", msg.account_id or msg.sender_id)
        route_metadata.setdefault("session_key", key)
        route_metadata.setdefault(
            "request_msg_id",
            str((msg.metadata or {}).get("request_msg_id") or (msg.metadata or {}).get("message_id") or ""),
        )
        route_metadata.setdefault(
            "run_id",
            str((msg.metadata or {}).get("request_msg_id") or (msg.metadata or {}).get("message_id") or uuid.uuid4()),
        )
        route_metadata.setdefault("bot_id", msg.bot_id or route_metadata.get("bot_id") or self._resolve_active_bot_id(route_metadata))
        route_metadata.setdefault(
            "_route_purpose",
            self._infer_route_purpose(
                content=msg.content,
                media=msg.media,
                metadata=msg.metadata,
            ).value,
        )
        if is_file_upload and not isinstance(interaction_context, dict):
            all_msgs = [
                *history,
                {"role": "user", "content": msg.content, "metadata": msg.metadata},
            ]
            self._save_turn(session, all_msgs, len(history))
            self._persist_session_state(sessions, memory_consolidator, session)
            self._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
            return None
        if self.tools.get("file_summary_skill") and self._is_file_summary_query(msg.content):
            summary_metadata = dict(msg.metadata or {}) if isinstance(msg.metadata, dict) else {}
            summary_metadata["_current_query"] = msg.content
            has_summary_file_context = bool(self._collect_file_entries_from_metadata(summary_metadata))
            summary_file_id = await self._resolve_file_id_for_summary(
                interaction_context=interaction_context if isinstance(interaction_context, dict) else None,
                metadata=summary_metadata,
                history=history,
                route_metadata=route_metadata,
            )
            if summary_file_id:
                if not is_file_upload:
                    await self._emit_progress(
                        on_progress or _bus_progress,
                        "识别到文件摘要请求，正在处理",
                        progress_percent=30,
                        progress_kind="tool_hint",
                        progress_stage="skill_call",
                        skill_name="file_summary_skill",
                        call_type="skill",
                        status_details={
                            "execution": {
                                "executor_skill": "file_summary_skill",
                                "file_id": summary_file_id,
                                "mode": "direct",
                            }
                        },
                    )
                tool_raw_result, tool_error = await self._execute_direct_tool_with_trace(
                    tool_name="file_summary_skill",
                    tool_args={"file_id": summary_file_id},
                    route_metadata=route_metadata,
                    on_progress=on_progress,
                    fallback_progress=_bus_progress,
                )
                if tool_error:
                    final_content = f"文件摘要执行失败：{tool_error}"
                    outbound_files = None
                else:
                    parsed_obj = self._extract_json_object(str(tool_raw_result or ""))
                    new_file_id = str((parsed_obj or {}).get("new_file_id") or "").strip()
                    final_content = (
                        f"文件摘要已完成，已生成新文件：{new_file_id}"
                        if new_file_id
                        else "文件摘要已完成。"
                    )
                    outbound_files = await self._build_outbound_file_entries(
                        [{"file_id": new_file_id}] if new_file_id else None,
                        tenant_id=str(route_metadata.get("tenant_id") or ""),
                        account_id=str(route_metadata.get("account_id") or ""),
                        bot_id=str(route_metadata.get("bot_id") or ""),
                    )
                reply_to = (
                    msg.reply_to
                    if hasattr(msg, "reply_to")
                    else None
                ) or route_metadata.get("request_msg_id") or route_metadata.get("parent_msg_id")
                all_msgs = [
                    *history,
                    {"role": "user", "content": msg.content, "metadata": msg.metadata},
                    {
                        "role": "tool",
                        "tool_call_id": "direct_file_summary_skill",
                        "name": "file_summary_skill",
                        "content": str(tool_raw_result or final_content),
                    },
                    {"role": "assistant", "content": final_content},
                ]
                outbound_metadata = _build_clean_outbound_metadata()
                if isinstance(outbound_files, list) and outbound_files:
                    outbound_metadata["files"] = outbound_files
                    outbound_metadata["content_type"] = "file"
                self._save_turn(session, all_msgs, len(history))
                self._persist_session_state(sessions, memory_consolidator, session)
                self._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=final_content,
                    account_id=msg.account_id,
                    tenant_id=msg.tenant_id,
                    bot_id=msg.bot_id,
                    reply_to=reply_to,
                    metadata=outbound_metadata,
                )
            if has_summary_file_context:
                # Legacy uploads may not have file_id; continue with normal graph/tool planning
                # instead of returning an immediate hard failure.
                pass
            else:
                final_content = "未找到可摘要的文件。请先上传文件，或在消息里带上 file_id / 附件后再试一次。"
                reply_to = (
                    msg.reply_to
                    if hasattr(msg, "reply_to")
                    else None
                ) or route_metadata.get("request_msg_id") or route_metadata.get("parent_msg_id")
                all_msgs = [
                    *history,
                    {"role": "user", "content": msg.content, "metadata": msg.metadata},
                    {"role": "assistant", "content": final_content},
                ]
                outbound_metadata = _build_clean_outbound_metadata()
                self._save_turn(session, all_msgs, len(history))
                self._persist_session_state(sessions, memory_consolidator, session)
                self._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=final_content,
                    account_id=msg.account_id,
                    tenant_id=msg.tenant_id,
                    bot_id=msg.bot_id,
                    reply_to=reply_to,
                    metadata=outbound_metadata,
                )
        direct_skill_check_graph = self._build_direct_skill_compliance_graph(msg.content)
        if direct_skill_check_graph is not None:
            direct_payload = {}
            if direct_skill_check_graph.get("nodes"):
                first_node = direct_skill_check_graph["nodes"][0]
                if isinstance(first_node, dict):
                    node_input = first_node.get("input")
                    if isinstance(node_input, dict):
                        direct_payload = dict(node_input)
            if not is_file_upload:
                await self._emit_progress(
                    on_progress or _bus_progress,
                    "正在执行技能规范检查",
                    progress_percent=35,
                    progress_kind="tool_hint",
                    progress_stage="skill_call",
                    skill_name="check_skill",
                    call_type="skill",
                    status_details={
                        "execution": {
                            "executor_skill": "check_skill",
                            "target_skill": direct_payload.get("skill_name"),
                            "mode": "direct",
                        }
                    },
                )
            skill_context = self._build_skill_context(
                route_metadata,
                on_progress=on_progress or _bus_progress,
                active_bot_id=route_metadata.get("bot_id"),
            )
            graph_trace = self._trace_direct_graph_shortcut_start(
                graph_spec=direct_skill_check_graph,
                route_metadata=route_metadata,
                active_bot_id=route_metadata.get("bot_id"),
                content_preview=msg.content,
            )
            final_result = await self._invoke_tool_via_plan(
                tool_name="check_skill",
                tool_args=direct_payload,
                skill_context=skill_context,
                route_metadata=route_metadata,
                on_progress=on_progress or _bus_progress,
                active_bot_id=route_metadata.get("bot_id"),
                record_assistant_tool_call=False,
            )
            final_content = final_result.content
            direct_success = final_result.success
            self._trace_direct_graph_shortcut_end(
                graph_trace=graph_trace,
                route_metadata=route_metadata,
                active_bot_id=route_metadata.get("bot_id"),
                success=direct_success,
                content_preview=final_content,
                error_text=final_result.error if not direct_success else None,
            )
            reply_to = (
                msg.reply_to
                if hasattr(msg, "reply_to")
                else None
            ) or route_metadata.get("request_msg_id") or route_metadata.get("parent_msg_id")
            all_msgs = [
                *history,
                {"role": "user", "content": msg.content, "metadata": msg.metadata},
                {"role": "assistant", "content": final_content},
            ]
            outbound_metadata = _build_clean_outbound_metadata()
            self._save_turn(session, all_msgs, len(history))
            self._persist_session_state(sessions, memory_consolidator, session)
            self._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=final_content,
                account_id=msg.account_id,
                tenant_id=msg.tenant_id,
                bot_id=msg.bot_id,
                reply_to=reply_to,
                metadata=outbound_metadata,
            )
        direct_route_result = await self._handle_registered_direct_routes(
            msg=msg,
            history=history,
            session=session,
            sessions=sessions,
            memory_consolidator=memory_consolidator,
            interaction_context=interaction_context if isinstance(interaction_context, dict) else None,
            on_progress=on_progress,
            fallback_progress=_bus_progress,
            build_clean_outbound_metadata=_build_clean_outbound_metadata,
        )
        if direct_route_result is not None:
            return direct_route_result
        # 2. Parsing phase
        if not is_file_upload:
            await self._emit_progress(
                on_progress or _bus_progress,
                "正在意图解析",
                progress_percent=15,
                progress_kind="status",
                progress_stage="parsing",
            )
        
        # Build messages for LLM
        initial_messages = context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            metadata=msg.metadata
        )
        checkpoint = self._load_runtime_checkpoint(key)
        interaction_response_obj = self._coerce_interaction_response(interaction_response, session_id=key)
        if (
            checkpoint is not None
            and checkpoint.status == RuntimeStatus.WAITING_HUMAN
            and interaction_response_obj is not None
            and checkpoint.pending_interaction is not None
            and checkpoint.pending_interaction.interaction_id == interaction_response_obj.interaction_id
            and checkpoint.messages_snapshot
        ):
            route_metadata["_runtime_resume"] = {
                "checkpoint_id": checkpoint.checkpoint_id,
                "run_id": checkpoint.run_id,
                "interaction_id": interaction_response_obj.interaction_id,
            }
            route_metadata["run_id"] = checkpoint.run_id
            initial_messages = list(checkpoint.messages_snapshot) + [
                {
                    "role": "user",
                    "content": self._build_interaction_resume_message(msg.content, interaction_response_obj),
                    "metadata": {"interaction_response": interaction_response_obj.to_dict()},
                }
            ]
            resumed_checkpoint = RuntimeCheckpoint.from_dict(
                {
                    **checkpoint.with_status(RuntimeStatus.RUNNING).to_dict(),
                    "pending_interaction": None,
                    "last_interaction_response": interaction_response_obj.to_dict(),
                }
            )
            self._save_runtime_checkpoint(resumed_checkpoint, expected_version=checkpoint.version)
        preflight_budget = self._prompt_budget_tokens()
        preflight_tokens, preflight_source = self._estimate_prompt_tokens(
            initial_messages,
            self.tools.get_definitions(),
            self.model,
        )
        logger.info(
            "Prompt preflight {}: estimated={}/{} via {}, history_msgs={}",
            key,
            preflight_tokens,
            preflight_budget,
            preflight_source,
            len(history),
        )
        record_trace_event(
            event_name="agent.prompt.preflight",
            phase="point",
            status="ok",
            component="agent_loop",
            source=msg.channel,
            account_id=msg.account_id or msg.sender_id,
            tenant_id=msg.tenant_id,
            bot_id=msg.bot_id,
            channel=msg.channel,
            chat_id=msg.chat_id,
            client_id=(msg.metadata or {}).get("client_id") if isinstance(msg.metadata, dict) else None,
            content_preview=msg.content,
            event_type="llm.prompt",
            details={
                "session_key": key,
                "history_messages": len(history),
                "prompt_tokens": preflight_tokens,
                "prompt_token_source": preflight_source,
                "prompt_budget_tokens": preflight_budget,
            },
        )

        if preflight_tokens > 0 and preflight_tokens > preflight_budget and history:
            snapshot = session.messages[session.last_consolidated:]
            logger.warning(
                "Session {} exceeds prompt budget before call: {}/{}. Resetting session history.",
                key,
                preflight_tokens,
                preflight_budget,
            )
            session.clear()
            sessions.save(session)
            if snapshot:
                self._schedule_background(memory_consolidator.archive_messages(snapshot))
            history = []
            route_metadata["_session_auto_reset"] = {
                "reason": "prompt_token_budget",
                "prompt_tokens": preflight_tokens,
                "prompt_budget_tokens": preflight_budget,
            }
            record_trace_event(
                event_name="agent.session.auto_reset",
                phase="point",
                status="ok",
                component="agent_loop",
                source=msg.channel,
                account_id=msg.account_id or msg.sender_id,
                tenant_id=msg.tenant_id,
                bot_id=msg.bot_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                client_id=(msg.metadata or {}).get("client_id") if isinstance(msg.metadata, dict) else None,
                content_preview=msg.content,
                event_type="session",
                details={
                    "session_key": key,
                    "reason": "prompt_token_budget",
                    "before_prompt_tokens": preflight_tokens,
                    "prompt_budget_tokens": preflight_budget,
                    "archived_messages": len(snapshot),
                },
            )
            initial_messages = context.build_messages(
                history=history,
                current_message=msg.content,
                media=msg.media if msg.media else None,
                channel=msg.channel,
                chat_id=msg.chat_id,
                metadata=msg.metadata,
            )

        graph_request = self._resolve_graph_request(msg.content, msg.metadata if isinstance(msg.metadata, dict) else None)
        if isinstance(graph_request, dict):
            route_metadata["_graph_request"] = dict(graph_request)
            reply_to = (
                msg.reply_to
                if hasattr(msg, "reply_to")
                else None
            ) or route_metadata.get("request_msg_id") or route_metadata.get("parent_msg_id")
            initial_state = dict((msg.metadata or {}).get("graph_initial_state") or {})
            if graph_request["mode"] == "preview":
                preview_graph = await self.plan_graph(
                    graph_request["query"],
                    on_progress=on_progress or _bus_progress,
                )
                final_content = self._build_graph_reply(None, preview_graph=preview_graph)
            elif graph_request["mode"] == "run_by_id":
                run = await self.run_graph_by_id(
                    graph_request["graph_id"],
                    metadata=route_metadata,
                    on_progress=on_progress or _bus_progress,
                    initial_state=initial_state,
                )
                final_content = self._build_graph_reply(run)
            else:
                run = await self.handle_request_via_graph(
                    graph_request["query"],
                    metadata=route_metadata,
                    on_progress=on_progress or _bus_progress,
                    initial_state=initial_state,
                )
                final_content = self._build_graph_reply(run)
            all_msgs = [
                *history,
                {"role": "user", "content": msg.content, "metadata": msg.metadata},
                {"role": "assistant", "content": final_content},
            ]
            outbound_metadata = _build_clean_outbound_metadata()
            for key in (
                "_bot_guardrails_turn_hits",
                "_bot_guardrails_last_hit",
                "_bot_guardrails_blocked",
                "_routing",
                "_graph_request",
                "_session_auto_reset",
            ):
                if key in route_metadata:
                    outbound_metadata[key] = route_metadata[key]
            self._save_turn(session, all_msgs, len(history))
            self._persist_session_state(sessions, memory_consolidator, session)
            self._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=final_content,
                account_id=msg.account_id,
                tenant_id=msg.tenant_id,
                bot_id=msg.bot_id,
                reply_to=reply_to,
                metadata=outbound_metadata,
            )
        
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            route_text=msg.content,
            route_metadata=route_metadata,
        )
        
        # 4. Finalizing phase
        if not is_file_upload:
            await self._emit_progress(
                on_progress or _bus_progress,
                "正在整理最终结果",
                progress_percent=95,
                progress_kind="status",
                progress_stage="finalizing",
            )

        if final_content is None:
            final_content = "已完成处理，但暂时没有可返回的结果。"

        outbound_metadata = _build_clean_outbound_metadata()
        for key in (
            "_bot_guardrails_turn_hits",
            "_bot_guardrails_last_hit",
            "_bot_guardrails_blocked",
            "_routing",
            "_session_auto_reset",
        ):
            if key in route_metadata:
                outbound_metadata[key] = route_metadata[key]
        if isinstance(route_metadata.get("_pending_interaction"), dict):
            outbound_metadata["interaction"] = dict(route_metadata["_pending_interaction"])
            outbound_metadata["status_event"] = "interaction"
        # 本回合真实 token 用量（多次 LLM 调用累计），供前端展示
        turn_usage = take_turn_usage()
        if turn_usage:
            outbound_metadata["usage"] = turn_usage

        self._save_turn(session, all_msgs, 1 + len(history))
        self._persist_session_state(sessions, memory_consolidator, session)
        self._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool):
            if mt.should_suppress_auto_reply(final_content):
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        
        # Attach collected media to outbound message
        from ithqbot import context as nb_context
        collected_media = nb_context.outbound_media.get()
        
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            account_id=msg.account_id, tenant_id=msg.tenant_id, bot_id=msg.bot_id,
            media=collected_media,
            metadata=outbound_metadata,
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        if not entry.get("metadata") and not entry.get("media"):
                            continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        if not entry.get("metadata") and not entry.get("media"):
                            continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    def _persist_session_state(
        self,
        sessions: SessionManager,
        memory_consolidator: MemoryConsolidator,
        session: Session,
    ) -> None:
        """Persist the updated session and any explicit user facts before returning."""
        memory_consolidator.persist_explicit_memory(session)
        sessions.save(session)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content, metadata=metadata or {})
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
