import time
from unittest.mock import Mock, patch

from ithqbot import context
from ithqbot.config.schema import RouteProfile, RoutePurpose
from ithqbot.observability import ObservabilityStore, build_routing_observability_payload


class _FakeRedis:
    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._strings: dict[str, str] = {}

    def pipeline(self):
        return self

    def lpush(self, key: str, value: str) -> None:
        self._lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key: str, start: int, end: int) -> None:
        rows = self._lists.get(key, [])
        self._lists[key] = rows[start : end + 1]

    def hincrby(self, key: str, field: str, amount: int) -> None:
        target = self._hashes.setdefault(key, {})
        current = int(target.get(field, "0"))
        target[field] = str(current + amount)

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self._hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        rows = self._lists.get(key, [])
        return rows[start : end + 1]

    def set(self, key: str, value: str, px: int | None = None) -> None:
        _ = px
        self._strings[key] = value

    def get(self, key: str) -> str | None:
        return self._strings.get(key)

    def execute(self) -> None:
        return None


def test_observability_audit_supports_per_bot_query():
    store = ObservabilityStore()
    store._client = _FakeRedis()
    tokens = context.set_runtime_context(
        account="acc_1",
        tenant="tenant_1",
        bot="bot_alpha",
        channel_name="icatmsg",
        chat="chat_1",
        metadata={"trace_id": "trace_1"},
    )
    try:
        store.record_audit(call_type="tool", name="web_fetch", phase="start", input_data={"q": "x"})
    finally:
        context.reset_runtime_context(tokens)

    all_rows = store.get_recent_audit_logs("acc_1", limit=10)
    bot_rows = store.get_recent_audit_logs("acc_1", bot_id="bot_alpha", limit=10)
    other_bot_rows = store.get_recent_audit_logs("acc_1", bot_id="bot_beta", limit=10)
    assert len(all_rows) == 1
    assert len(bot_rows) == 1
    assert other_bot_rows == []


def test_observability_llm_stats_supports_account_and_bot_dimension():
    store = ObservabilityStore()
    store._client = _FakeRedis()
    tokens = context.set_runtime_context(
        account="acc_2",
        tenant="tenant_1",
        bot="bot_alpha",
        channel_name="icatmsg",
        chat="chat_2",
        metadata={"trace_id": "trace_2"},
    )
    try:
        store.record_llm_usage(
            source="tool",
            model="model_x",
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            finish_reason="stop",
            component="web_fetch",
        )
    finally:
        context.reset_runtime_context(tokens)

    account_stats = store.get_account_llm_stats("acc_2")
    bot_stats = store.get_account_llm_stats("acc_2", bot_id="bot_alpha")
    assert account_stats["tool"]["request_count"] == 1
    assert account_stats["tool"]["request_tokens"] == 3
    assert account_stats["tool"]["response_tokens"] == 4
    assert bot_stats["tool"]["request_count"] == 1
    assert bot_stats["tool"]["total_tokens"] == 7


def test_trace_events_fallback_to_in_memory_without_redis():
    store = ObservabilityStore()

    store.record_trace_event(
        event_name="client.message.accepted",
        phase="start",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_1",
        trace_id="trace_1",
        content_preview="hello",
    )
    store.record_trace_event(
        event_name="server.client.delivered",
        phase="end",
        status="completed",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_1",
        trace_id="trace_1",
        content_preview="world",
    )

    tasks = store.get_trace_tasks(
        account_id="13955168586",
        bot_id="bot_alpha",
        chat_id="bot_alpha",
        client_id="web_client_1",
        channel="icatmsg",
        limit=10,
    )
    detail = store.get_trace_task("req_1")
    events = store.get_trace_task_events("req_1", limit=10)

    assert len(tasks) == 1
    assert detail is not None
    assert len(events) == 2


def test_build_routing_observability_payload_normalizes_profile_and_state() -> None:
    payload = build_routing_observability_payload(
        {"purpose": "planner", "final_model": "qwen3-72B", "fallback_count": 1, "fallback_used": True},
        route_profile=RouteProfile(
            purpose=RoutePurpose.FINAL_ANSWER,
            active_model="minimax/minimax2.5-100B",
            fallback_models=["qwen3-27B"],
            max_tokens=4096,
            reasoning_effort="high",
            source="classifier",
            tier="large",
        ),
        task="final_answer",
        skill_name="demo_skill",
    )

    assert payload["source"] == "classifier"
    assert payload["task"] == "final_answer"
    assert payload["skill_name"] == "demo_skill"
    assert payload["selected_purpose"] == "planner"
    assert payload["current_model"] == "qwen3-72B"
    assert payload["fallback_models"] == ["qwen3-27B"]
    assert payload["fallback_count"] == 1
    assert payload["fallback_used"] is True
    assert payload["max_tokens"] == 4096


def test_configure_redis_client_uses_short_timeouts():
    store = ObservabilityStore()
    client = Mock()
    client.ping.return_value = True

    with patch("ithqbot.observability.redis.Redis.from_url", return_value=client) as from_url:
        store.configure("redis://localhost:6379/0")

    kwargs = from_url.call_args.kwargs
    assert kwargs["socket_connect_timeout"] == 0.2
    assert kwargs["socket_timeout"] == 0.2


def test_trace_summary_aggregates_status_and_duration():
    store = ObservabilityStore()

    store.record_trace_event(
        event_name="client.message.accepted",
        phase="start",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_2",
        trace_id="trace_2",
        content_preview="task a",
    )
    time.sleep(0.002)
    store.record_trace_event(
        event_name="server.client.delivered",
        phase="end",
        status="completed",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_2",
        trace_id="trace_2",
        content_preview="done a",
    )
    store.record_trace_event(
        event_name="client.message.accepted",
        phase="start",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_3",
        trace_id="trace_3",
        content_preview="task b",
    )
    store.record_trace_event(
        event_name="agent.task.failed",
        phase="failed",
        status="failed",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_3",
        trace_id="trace_3",
        content_preview="failed b",
    )

    summary = store.get_trace_summary(
        account_id="13955168586",
        bot_id="bot_alpha",
        chat_id="bot_alpha",
        client_id="web_client_1",
        channel="icatmsg",
        limit=10,
    )

    assert summary["total_tasks"] == 2
    assert summary["delivered_tasks"] == 1
    assert summary["failed_tasks"] == 1
    assert summary["status_breakdown"]["delivered"] == 1
    assert summary["status_breakdown"]["failed"] == 1
    assert summary["avg_event_count"] == 2
    assert summary["avg_duration_ms"] >= 0
    assert summary["p95_duration_ms"] >= 0


def test_observability_admin_queries_require_allowlisted_requester() -> None:
    store = ObservabilityStore()
    store._client = _FakeRedis()
    store._config = type("Cfg", (), {"admin_account_ids": ["admin_1"], "redis_retention_h": 24})()

    store.record_trace_event(
        event_name="client.message.accepted",
        phase="start",
        account_id="user_1",
        tenant_id="tenant_a",
        request_msg_id="req_admin_scope",
        trace_id="trace_admin_scope",
    )

    denied = store.get_trace_tasks(is_admin=True, requester_account_id="user_1", limit=10)
    allowed = store.get_trace_tasks(is_admin=True, requester_account_id="admin_1", limit=10)

    assert denied == []
    assert len(allowed) == 1
    assert allowed[0]["request_msg_id"] == "req_admin_scope"


def test_observability_admin_single_task_can_resolve_tenant_without_hint() -> None:
    store = ObservabilityStore()
    store._client = _FakeRedis()
    store._config = type("Cfg", (), {"admin_account_ids": ["admin_1"], "redis_retention_h": 24})()

    store.record_trace_event(
        event_name="client.message.accepted",
        phase="start",
        account_id="user_2",
        tenant_id="tenant_b",
        request_msg_id="req_lookup",
        trace_id="trace_lookup",
    )

    detail = store.get_trace_task(
        "req_lookup",
        is_admin=True,
        requester_account_id="admin_1",
    )
    events = store.get_trace_task_events(
        "req_lookup",
        is_admin=True,
        requester_account_id="admin_1",
        limit=10,
    )

    assert detail is not None
    assert detail["tenant_id"] == "tenant_b"
    assert [event["event_name"] for event in events] == ["client.message.accepted"]


def test_trace_summary_and_tree_include_graph_dimensions():
    store = ObservabilityStore()

    store.record_trace_event(
        event_name="graph.run.start",
        phase="start",
        status="running",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_graph",
        trace_id="trace_graph",
        details={
            "graph_id": "weekly_report",
            "run_id": "graph-run-1",
            "planner_attempt": 2,
            "idempotent": True,
            "retryable": True,
        },
    )
    store.record_trace_event(
        event_name="graph.node.done",
        phase="end",
        status="ok",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_graph",
        trace_id="trace_graph",
        parent_run_id="graph-run-1",
        details={
            "graph_id": "weekly_report",
            "run_id": "graph-run-1:step1",
            "node_id": "step1",
            "parent_run_id": "graph-run-1",
            "planner_attempt": 2,
            "idempotent": True,
            "retryable": True,
        },
    )

    task = store.get_trace_task("req_graph")
    tree = store.get_trace_tree("req_graph")
    summary = store.get_trace_summary(account_id="13955168586", bot_id="bot_alpha", limit=10)

    assert task["graph_id"] == "weekly_report"
    assert task["run_id"] == "graph-run-1:step1"
    assert task["node_id"] == "step1"
    assert task["planner_attempt"] == 2
    assert task["idempotent"] is True
    assert task["retryable"] is True
    assert tree["tree"][0]["id"] == "graph-run-1"
    assert tree["tree"][0]["children"][0]["id"] == "graph-run-1:step1"
    assert summary["graph_tasks"] == 1
    assert summary["planner_retry_tasks"] == 1
    assert summary["retryable_tasks"] == 1
    assert summary["idempotent_tasks"] == 1
    assert summary["top_graphs"] == [{"name": "weekly_report", "count": 1}]


def test_record_trace_event_tolerates_null_status_graph_in_details() -> None:
    store = ObservabilityStore()

    store.record_trace_event(
        event_name="agent.direct_route",
        phase="running",
        status="running",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_direct_route",
        trace_id="trace_direct_route",
        details={
            "tool_name": "doc_compare",
            "handler_source": "ithqbot.skills.doc_compare.direct_handler.handle_direct_doc_compare",
            "status_details": {
                "graph": None,
            },
        },
    )

    task = store.get_trace_task("req_direct_route")
    events = store.get_trace_task_events("req_direct_route", limit=10)

    assert task is not None
    assert task["request_msg_id"] == "req_direct_route"
    assert [event["event_name"] for event in events] == ["agent.direct_route"]


def test_trace_graph_payload_includes_topology_and_node_status() -> None:
    store = ObservabilityStore()

    store.record_trace_event(
        event_name="graph.run.start",
        phase="start",
        status="running",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_graph_view",
        trace_id="trace_graph_view",
        details={
            "graph_id": "approval_flow",
            "run_id": "graph-run-2",
            "graph": {
                "graph_id": "approval_flow",
                "run_id": "graph-run-2",
                "status": "running",
                "node_count": 2,
                "completed_nodes": 1,
                "running_nodes": 1,
                "current_node_ids": ["approve"],
                "topology": {
                    "nodes": [
                        {"node_id": "draft", "skill_name": "writer", "status": "done"},
                        {"node_id": "approve", "skill_name": "reviewer", "status": "running"},
                    ],
                    "edges": [{"from": "draft", "to": "approve", "kind": "dependency"}],
                },
            },
        },
    )
    store.record_trace_event(
        event_name="graph.node.done",
        phase="end",
        status="ok",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_graph_view",
        trace_id="trace_graph_view",
        parent_run_id="graph-run-2",
        duration_ms=120,
        details={
            "graph_id": "approval_flow",
            "run_id": "graph-run-2:draft",
            "node_id": "draft",
            "parent_run_id": "graph-run-2",
            "node": {
                "node_id": "draft",
                "skill_name": "writer",
                "status": "done",
                "state_update_keys": ["draft_text"],
            },
        },
    )
    store.record_trace_event(
        event_name="graph.node.waiting",
        phase="end",
        status="waiting",
        account_id="13955168586",
        bot_id="bot_alpha",
        channel="icatmsg",
        chat_id="bot_alpha",
        client_id="web_client_1",
        request_msg_id="req_graph_view",
        trace_id="trace_graph_view",
        parent_run_id="graph-run-2",
        duration_ms=30,
        details={
            "graph_id": "approval_flow",
            "run_id": "graph-run-2:approve",
            "node_id": "approve",
            "parent_run_id": "graph-run-2",
            "node": {
                "node_id": "approve",
                "skill_name": "reviewer",
                "status": "waiting",
                "waiting_for_input": True,
                "interaction": {"type": "form", "title": "审批"},
                "input_mapping_keys": ["draft_text"],
                "output_mapping_keys": ["approved"],
            },
        },
    )

    graph = store.get_trace_graph("req_graph_view")

    assert graph["graph_id"] == "approval_flow"
    assert graph["run_id"] == "graph-run-2"
    assert graph["status"] == "waiting"
    assert graph["summary"]["node_count"] == 2
    assert graph["summary"]["completed_nodes"] == 1
    assert graph["summary"]["running_nodes"] == 0
    assert graph["summary"]["current_node_ids"] == ["approve"]
    assert graph["edges"] == [{"from": "draft", "to": "approve", "condition": None, "kind": "dependency"}]
    assert [node["node_id"] for node in graph["nodes"]] == ["draft", "approve"]
    assert graph["nodes"][0]["state_update_keys"] == ["draft_text"]
    assert graph["nodes"][1]["waiting_for_input"] is True
    assert graph["nodes"][1]["interaction"]["title"] == "审批"


def test_noisy_trace_events_can_be_sampled_out() -> None:
    store = ObservabilityStore()
    store._config = type(
        "Cfg",
        (),
        {
            "enabled": True,
            "async_mode": False,
            "admin_account_ids": [],
            "trace_sample_rate": 0.0,
            "trace_noise_suppression_window_ms": 0,
            "noisy_trace_events": ["skill.progress"],
            "trace_sample_exempt_events": [],
        },
    )()

    store.record_trace_event(
        event_name="skill.progress",
        phase="running",
        status="running",
        account_id="u1",
        request_msg_id="req-sampled",
    )
    assert store.get_trace_task("req-sampled") is None


def test_noisy_trace_events_can_be_suppressed_by_window() -> None:
    store = ObservabilityStore()
    store._config = type(
        "Cfg",
        (),
        {
            "enabled": True,
            "async_mode": False,
            "admin_account_ids": [],
            "trace_sample_rate": 1.0,
            "trace_noise_suppression_window_ms": 60_000,
            "noisy_trace_events": ["skill.progress"],
            "trace_sample_exempt_events": [],
        },
    )()

    store.record_trace_event(
        event_name="skill.progress",
        phase="running",
        status="running",
        account_id="u1",
        request_msg_id="req-noise",
    )
    store.record_trace_event(
        event_name="skill.progress",
        phase="running",
        status="running",
        account_id="u1",
        request_msg_id="req-noise",
    )
    events = store.get_trace_task_events("req-noise", limit=10)
    assert len(events) == 1
