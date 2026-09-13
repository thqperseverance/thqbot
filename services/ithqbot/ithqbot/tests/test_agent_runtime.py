import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import ithqbot.agent.loop as loop_module
from ithqbot.agent.loop import AgentLoop
from ithqbot.agent.runtime import (
    CancellationTokenSource,
    ExecutorConfig,
    ExecutorState,
    FileRuntimeCheckpointStore,
    PlannerIterationState,
    RuntimeExecutionWorker,
    RuntimeIngressAdapter,
    RuntimeCheckpoint,
    RuntimeScheduler,
    RuntimeStatus,
    ToolCallPlan,
)
from ithqbot.bus.events import InboundMessage
from ithqbot.agent.tools.base import Tool, ToolResult
from ithqbot.agent.tools.registry import ToolRegistry
from ithqbot.bus.queue import MessageBus
from ithqbot.config.schema import RouteProfile, RoutePurpose
from ithqbot.providers.base import LLMResponse, ToolCallRequest
from ithqbot.session.manager import SessionManager
from ithqbot.session.store import FileSessionStore


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo_tool"

    @property
    def description(self) -> str:
        return "echo tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> str:
        return str(kwargs.get("text") or "")


class CancellableEchoTool(Tool):
    @property
    def name(self) -> str:
        return "cancellable_echo"

    @property
    def description(self) -> str:
        return "echo with cancellation token"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> str:
        token = kwargs.get("cancellation_token")
        if token is not None and hasattr(token, "throw_if_cancelled"):
            token.throw_if_cancelled()
        return str(kwargs.get("text") or "")


def _make_loop(tmp_path: Path, **kwargs: Any) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    kwargs.setdefault("session_manager", SessionManager(workspace=tmp_path, store=FileSessionStore(tmp_path)))
    kwargs.setdefault("tool_registrars", [])
    with patch("ithqbot.agent.loop.FileService"), patch("ithqbot.agent.loop.FileServiceConfig"):
        return AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            **kwargs,
        )


def test_tool_call_plan_builds_linear_dependencies() -> None:
    plan = ToolCallPlan.from_tool_calls(
        [
            ToolCallRequest(id="call_1", name="tool_a", arguments={"a": 1}),
            ToolCallRequest(id="call_2", name="tool_b", arguments={"b": 2}),
            ToolCallRequest(id="call_3", name="tool_c", arguments={"c": 3}),
        ]
    )

    assert [node.tool_name for node in plan.nodes] == ["tool_a", "tool_b", "tool_c"]
    assert plan.nodes[0].depends_on == ()
    assert plan.nodes[1].depends_on == ("call_1",)
    assert plan.nodes[2].depends_on == ("call_2",)


def test_tool_call_plan_signature_is_stable_for_dependency_ir() -> None:
    plan = ToolCallPlan.from_tool_calls(
        [
            ToolCallRequest(id="call_1", name="tool_a", arguments={"b": 2, "a": 1}),
            ToolCallRequest(id="call_2", name="tool_b", arguments={"x": "y"}),
        ]
    )

    assert plan.signature() == (
        'tool_a:{"a": 1, "b": 2}',
        'tool_b:{"x": "y"}',
    )


def test_tool_call_plan_to_record_keeps_replay_contract() -> None:
    plan = ToolCallPlan.single(
        tool_name="tool_a",
        arguments={"a": 1},
        tool_call_id="call_1",
    )

    record = plan.to_record(
        inputs={"route_text": "do tool_a"},
        outputs={"node_results": {"call_1": {"content": "ok", "success": True, "error": None, "metadata": {}}}},
    )

    assert record["schema_version"] == "v1"
    assert record["plan_version"] == "1.0"
    assert record["signature"] == ('tool_a:{"a": 1}',)
    assert record["plan"]["nodes"][0]["tool_name"] == "tool_a"
    assert record["inputs"]["route_text"] == "do tool_a"


def test_tool_invoke_returns_tool_result_contract() -> None:
    result = asyncio.run(EchoTool().invoke({"text": "hello"}))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.content == "hello"


def test_tool_invoke_converts_validation_error_to_tool_result() -> None:
    result = asyncio.run(EchoTool().invoke({"text": 123}))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.content == "123"


def test_tool_invoke_honors_cancellation_token() -> None:
    token_source = CancellationTokenSource()
    token_source.cancel("user_cancelled")

    result = asyncio.run(
        CancellableEchoTool().invoke(
            {"text": "hello"},
            cancellation_token=token_source.token,
        )
    )

    assert result.success is False
    assert "执行工具" in result.content


def test_agent_loop_supports_external_tool_registry_injection(tmp_path: Path) -> None:
    registry = ToolRegistry()

    def register_echo(reg: ToolRegistry) -> None:
        reg.register(EchoTool())

    loop = _make_loop(
        tmp_path,
        tool_registry=registry,
        tool_registrars=[register_echo],
    )

    assert loop.tools is registry
    assert loop.tools.has("echo_tool")


def test_agent_loop_uses_default_executor_config_when_not_provided(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    assert loop.executor.config == ExecutorConfig()


def test_runtime_checkpoint_roundtrip(tmp_path: Path) -> None:
    store = FileRuntimeCheckpointStore(tmp_path)
    checkpoint = RuntimeCheckpoint.create(
        session_id="icatmsg:t:a:c:bot",
        run_id="req-1",
        trace_id="req-1",
        route_metadata={"session_key": "icatmsg:t:a:c:bot"},
        status=RuntimeStatus.RUNNING,
    )

    saved = store.save(checkpoint)
    loaded = store.load("icatmsg:t:a:c:bot")

    assert saved.version == 1
    assert loaded is not None
    assert loaded.run_id == "req-1"
    assert loaded.status == RuntimeStatus.RUNNING


def test_runtime_checkpoint_preserves_pending_human_interaction(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    interaction = loop._normalize_interaction_request(
        {
            "interaction_id": "otp-1",
            "type": "otp",
            "title": "验证码",
            "fields": [{"key": "otp_code", "label": "验证码"}],
        },
        session_id="icatmsg:t:a:c:bot",
    )
    checkpoint = RuntimeCheckpoint.create(
        session_id="icatmsg:t:a:c:bot",
        run_id="req-1",
        trace_id="req-1",
        route_metadata={"session_key": "icatmsg:t:a:c:bot"},
        status=RuntimeStatus.WAITING_HUMAN,
    )
    persisted = loop._save_runtime_checkpoint(
        RuntimeCheckpoint.from_dict(
            {
                **checkpoint.to_dict(),
                "pending_interaction": interaction.to_dict() if interaction else None,
            }
        )
    )
    loaded = loop._load_runtime_checkpoint("icatmsg:t:a:c:bot")

    assert persisted is not None
    assert loaded is not None
    assert loaded.status == RuntimeStatus.WAITING_HUMAN
    assert loaded.pending_interaction is not None
    assert loaded.pending_interaction.interaction_id == "otp-1"


def test_runtime_scheduler_routes_ingress_to_execution(tmp_path: Path) -> None:
    bus = MessageBus()
    adapter = RuntimeIngressAdapter(bus)
    scheduler = RuntimeScheduler(bus)
    handled: list[str] = []

    async def handler(command):
        handled.append(command.command_id)

    worker = RuntimeExecutionWorker(bus, handler)
    inbound = InboundMessage(
        channel="icatmsg",
        sender_id="u1",
        chat_id="c1",
        content="hello",
        metadata={"request_msg_id": "req-1"},
    )

    async def scenario() -> None:
        scheduler_task = asyncio.create_task(scheduler.run())
        worker_task = asyncio.create_task(worker.run())
        await adapter.publish_message(inbound)
        await asyncio.sleep(0.05)
        scheduler.stop()
        worker.stop()
        scheduler_task.cancel()
        worker_task.cancel()
        await asyncio.gather(scheduler_task, worker_task, return_exceptions=True)

    asyncio.run(scenario())

    assert handled == ["req-1"]


def test_policy_turn_exposes_decision_trace(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.model_router.decide = AsyncMock(
        return_value=type(
            "Decision",
            (),
            {
                "model": "test-model",
                "fallback_models": [],
                "source": "default",
                "tier": "standard",
                "max_tokens": 256,
                "reasoning_effort": None,
                "purpose": RoutePurpose.PLANNER,
            },
        )()
    )

    turn = asyncio.run(
        loop.policy.resolve_turn(
            route_text="please search",
            route_metadata={"bot_id": "bot-a"},
            tool_defs=[
                {"type": "function", "function": {"name": "echo_tool", "parameters": {}}},
            ],
        )
    )

    assert "route: bot-a" in turn.decision_trace
    assert "purpose: planner" in turn.decision_trace
    assert "guardrails: allow=[echo_tool]" in turn.decision_trace


def test_policy_uses_requested_route_purpose(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.model_router.decide = AsyncMock(
        return_value=RouteProfile(
            purpose=RoutePurpose.VISION,
            active_model="vision-model",
            fallback_models=["vision-backup"],
            max_tokens=512,
            source="default",
            tier="medium",
        )
    )

    turn = asyncio.run(
        loop.policy.resolve_turn(
            route_text="请看看这张图里有什么",
            route_metadata={"_route_purpose": "vision"},
            tool_defs=[],
        )
    )

    assert turn.decision is not None
    assert turn.decision.purpose == RoutePurpose.VISION
    assert "purpose: vision" in turn.decision_trace
    assert loop.model_router.decide.await_args.kwargs["purpose"] == RoutePurpose.VISION


def test_policy_record_routing_metric_delegates_to_loop(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    loop.policy.record_routing_metric("medium", "fallbacks")

    assert loop._routing_metrics["medium"]["fallbacks"] == 1


def test_loop_infers_extraction_and_vision_purposes(tmp_path: Path) -> None:
    class FileSummaryTool(Tool):
        @property
        def name(self) -> str:
            return "file_summary_skill"

        @property
        def description(self) -> str:
            return "summarize a file"

        @property
        def parameters(self) -> dict[str, Any]:
            return {"type": "object", "properties": {"file_id": {"type": "string"}}}

        async def execute(self, **kwargs: Any) -> str:
            return "ok"

    loop = _make_loop(tmp_path)
    loop.tools.register(FileSummaryTool())

    assert (
        loop._infer_route_purpose(
            content="总结一下这个附件",
            media=[],
            metadata={},
        )
        == RoutePurpose.EXTRACTION
    )
    assert (
        loop._infer_route_purpose(
            content="看看图片里有什么",
            media=["/tmp/photo.jpg"],
            metadata={},
        )
        == RoutePurpose.VISION
    )


def test_loop_normalizes_routing_state_to_stable_fields(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    routing = loop._normalize_routing_state(
        {
            "purpose": "planner",
            "tier": "medium",
            "model": "planner-model",
            "final_model": "final-model",
        }
    )

    assert routing["requested_purpose"] == "planner"
    assert routing["selected_purpose"] == "planner"
    assert routing["current_purpose"] == "planner"
    assert routing["initial_model"] == "planner-model"
    assert routing["current_model"] == "final-model"
    assert routing["final_model"] == "final-model"
    assert routing["initial_tier"] == "medium"
    assert routing["current_tier"] == "medium"


def test_graph_planner_uses_planner_route_profile(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.model_router.decide = AsyncMock(
        return_value=RouteProfile(
            purpose=RoutePurpose.PLANNER,
            active_model="planner-route-model",
            fallback_models=["planner-route-backup"],
            max_tokens=768,
            reasoning_effort="medium",
            source="default",
            tier="medium",
        )
    )
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content='{"graph_id":"g1","nodes":[],"edges":[]}', tool_calls=[])
    )

    planner = loop._get_graph_planner()
    result = asyncio.run(planner.llm("帮我规划一个技能图"))

    assert '"graph_id":"g1"' in result
    assert loop.model_router.decide.await_args.kwargs["purpose"] == RoutePurpose.PLANNER
    assert loop.provider.chat_with_retry.await_args.kwargs["model"] == "planner-route-model"
    assert loop.provider.chat_with_retry.await_args.kwargs["max_tokens"] == 768


def test_planner_reroutes_to_final_answer_after_tool_results(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="最终答复", tool_calls=[]))
    loop.model_router.decide = AsyncMock(
        return_value=RouteProfile(
            purpose=RoutePurpose.FINAL_ANSWER,
            active_model="final-answer-model",
            fallback_models=["final-answer-backup"],
            max_tokens=1024,
            reasoning_effort="high",
            source="classifier",
            tier="large",
        )
    )
    route_metadata = {"_routing": {"purpose": "planner"}}
    fallback_models = ["planner-backup"]
    initial_decision = RouteProfile(
        purpose=RoutePurpose.PLANNER,
        active_model="planner-model",
        fallback_models=["planner-backup"],
        max_tokens=256,
        source="default",
        tier="medium",
    )

    result = asyncio.run(
        loop.planner.plan_iteration(
            messages=[{"role": "tool", "tool_call_id": "call_1", "name": "echo_tool", "content": "ok"}],
            guarded_tool_defs=[],
            active_model="planner-model",
            route_text="请基于工具结果给出最终答复",
            route_metadata=route_metadata,
            iteration=1,
            decision=initial_decision,
            fallback_models=fallback_models,
            state=PlannerIterationState(),
        )
    )

    assert result.response is not None
    assert result.active_model == "final-answer-model"
    assert fallback_models == ["final-answer-backup"]
    assert route_metadata["_routing"]["purpose"] == "planner"
    assert route_metadata["_routing"]["selected_purpose"] == "planner"
    assert route_metadata["_routing"]["current_purpose"] == "final_answer"
    assert route_metadata["_routing"]["current_model"] == "final-answer-model"
    assert route_metadata["_routing"]["final_model"] == "final-answer-model"
    assert loop.model_router.decide.await_args.kwargs["purpose"] == RoutePurpose.FINAL_ANSWER
    assert loop.provider.chat_with_retry.await_args.kwargs["model"] == "final-answer-model"
    assert loop.provider.chat_with_retry.await_args.kwargs["reasoning_effort"] == "high"


def test_agent_trace_events_include_stable_routing_payload(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    route_metadata = {"bot_id": "bot_a", "tenant_id": "tenant_x", "account_id": "acc_x"}
    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    with patch.object(loop_module, "record_trace_event", _capture):
        final_content, _, _ = asyncio.run(
            loop._run_agent_loop(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}],
                route_text="hello",
                route_metadata=route_metadata,
            )
        )

    assert final_content == "ok"
    route_event = next(item for item in captured if item.get("event_name") == "agent.route.decided")
    reply_event = next(item for item in captured if item.get("event_name") == "agent.reply.generated")
    assert route_event["details"]["routing"]["selected_purpose"] == "planner"
    assert route_event["details"]["routing"]["current_model"]
    assert reply_event["details"]["routing"]["final_model"]
    assert reply_event["details"]["routing"]["success"] is True
