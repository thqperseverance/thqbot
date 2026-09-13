from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ithqbot import context
from ithqbot.agent.loop import AgentLoop
from ithqbot.agent.skills.base import SkillContext
from ithqbot.agent.skills.loader import SkillsLoader
from ithqbot.agent.tools.base import Tool
from ithqbot.bus.queue import MessageBus
from ithqbot.graph import (
    GraphExecutor,
    GraphRun,
    InteractionRequired,
    Node,
    StateManager,
    get_ready_nodes,
    load_graph,
    load_graph_by_id,
    load_graph_data,
    load_graph_from_dict,
)
from ithqbot.observability import get_trace_task_events, get_trace_tree
from ithqbot.planner import (
    EXAMPLES,
    GraphPlanner,
    SkillInfo,
    build_prompt,
    build_skill_text,
    load_skills,
    safe_plan,
    validate_graph,
)
from ithqbot.providers.base import LLMResponse


class FakeCursor:
    def __init__(self, store: dict):
        self.store = store
        self._row = None
        self._rows = []

    def execute(self, query, params=None):
        sql = " ".join(str(query).split()).lower()
        params = params or ()
        if sql.startswith("create table if not exists graph_runs"):
            return None
        if sql.startswith("create table if not exists graph_nodes"):
            return None
        if sql.startswith("insert into graph_runs"):
            run_id, graph_id, status, state = params
            self.store["graph_runs"][run_id] = {
                "run_id": run_id,
                "graph_id": graph_id,
                "status": status,
                "state": state,
            }
            return None
        if sql.startswith("select run_id, graph_id, status, state from graph_runs"):
            row = self.store["graph_runs"].get(params[0])
            self._row = None if row is None else (row["run_id"], row["graph_id"], row["status"], row["state"])
            return None
        if sql.startswith("insert into graph_nodes"):
            run_id, node_id, status, node_input, output = params
            self.store["graph_nodes"][(run_id, node_id)] = {
                "run_id": run_id,
                "node_id": node_id,
                "status": status,
                "input": node_input,
                "output": output,
            }
            return None
        if sql.startswith("select node_id, status, input, output from graph_nodes"):
            run_id = params[0]
            self._rows = [
                (row["node_id"], row["status"], row["input"], row["output"])
                for (saved_run_id, _), row in self.store["graph_nodes"].items()
                if saved_run_id == run_id
            ]
            return None
        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class FakeConnection:
    def __init__(self):
        self.store = {"graph_runs": {}, "graph_nodes": {}}

    def cursor(self):
        return FakeCursor(self.store)

    def close(self):
        return None


class ConcurrentRunner:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def run(self, skill_name, payload, context):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if skill_name != "join":
            await asyncio.sleep(0.05)
        self.active -= 1
        return {"status": "success", "data": {skill_name: payload.get("seed", skill_name)}}


class InteractionRunner:
    def __init__(self):
        self.calls = []

    async def run(self, skill_name, payload, context):
        self.calls.append((skill_name, dict(payload), context.skill_name))
        if skill_name == "ask_user" and "answer" not in payload:
            raise InteractionRequired(
                {
                    "type": "form",
                    "title": "补充信息",
                    "prompt": "请输入 answer",
                    "fields": [{"key": "answer", "label": "answer", "input_type": "text"}],
                }
            )
        if skill_name == "ask_user":
            return {"status": "success", "data": {"answer": payload["answer"]}}
        return {"status": "success", "data": {"final_answer": payload["answer"]}}


class ContextCapturingRunner:
    def __init__(self):
        self.contexts = []

    async def run(self, skill_name, payload, context):
        self.contexts.append(
            {
                "skill_name": context.skill_name,
                "tenant_id": context.tenant_id,
                "account_id": context.account_id,
                "chat_id": context.chat_id,
                "bot_id": context.bot_id,
                "parent_run_id": context.parent_run_id,
                "metadata": dict(context.metadata),
            }
        )
        return {"status": "success", "data": {"handled_by": skill_name}}


def make_context() -> SkillContext:
    return SkillContext(
        tenant_id="tenant",
        account_id="account",
        chat_id="chat",
        bot_id="bot",
        metadata={"source": "test"},
    )


def make_trace_context(*, emit_callback=None, request_msg_id: str | None = None) -> SkillContext:
    request_id = request_msg_id or f"req-{uuid.uuid4().hex}"
    return SkillContext(
        tenant_id="tenant",
        account_id="account",
        chat_id="chat",
        bot_id="bot",
        client_id="client",
        trace_id=f"trace-{request_id}",
        request_msg_id=request_id,
        metadata={"source": "test"},
        emit_callback=emit_callback,
    )


def test_load_graph_from_yaml(tmp_path) -> None:
    graph_file = tmp_path / "graph.yaml"
    graph_file.write_text(
        """
graph_id: test_flow
nodes:
  - id: step1
    skill: echo_skill
  - id: step2
    skill: echo_skill
edges:
  - from: step1
    to: step2
""".strip(),
        encoding="utf-8",
    )

    graph = load_graph(str(graph_file))

    assert graph.graph_id == "test_flow"
    assert set(graph.nodes) == {"step1", "step2"}
    assert graph.edges[0].from_node == "step1"
    assert graph.edges[0].to_node == "step2"


def test_get_ready_nodes_respects_condition_state() -> None:
    graph = load_graph_data(
        {
            "graph_id": "conditional_flow",
            "nodes": [
                {"id": "root", "skill": "seed"},
                {"id": "approved", "skill": "approve"},
                {"id": "rejected", "skill": "reject"},
            ],
            "edges": [
                {"from": "root", "to": "approved", "condition": "state['approved'] is True"},
                {"from": "root", "to": "rejected", "condition": "state['approved'] is False"},
            ],
        }
    )
    graph.nodes["root"].status = "done"
    run = GraphRun(run_id="run-1", graph_id=graph.graph_id, status="running", state={"approved": True})

    ready = get_ready_nodes(graph, run)

    assert [node.id for node in ready] == ["approved"]


def test_get_ready_nodes_supports_json_style_boolean_conditions() -> None:
    graph = load_graph_data(
        {
            "graph_id": "conditional_json_flow",
            "nodes": [
                {"id": "root", "skill": "seed"},
                {"id": "approved", "skill": "approve"},
                {"id": "rejected", "skill": "reject"},
            ],
            "edges": [
                {"from": "root", "to": "approved", "condition": "state.approved == true"},
                {"from": "root", "to": "rejected", "condition": "state.approved == false"},
            ],
        }
    )
    graph.nodes["root"].status = "done"
    run = GraphRun(run_id="run-json-1", graph_id=graph.graph_id, status="running", state={"approved": True})

    ready = get_ready_nodes(graph, run)

    assert [node.id for node in ready] == ["approved"]


@pytest.mark.asyncio
async def test_graph_executor_marks_false_branch_as_skipped() -> None:
    graph = load_graph_data(
        {
            "graph_id": "branch_flow",
            "nodes": [
                {"id": "start", "skill": "start"},
                {"id": "approved", "skill": "approved"},
                {"id": "rejected", "skill": "rejected"},
            ],
            "edges": [
                {"from": "start", "to": "approved", "condition": "state['approved'] is True"},
                {"from": "start", "to": "rejected", "condition": "state['approved'] is False"},
            ],
        }
    )
    state_manager = StateManager(FakeConnection())

    class BranchRunner:
        async def run(self, skill_name, payload, context):
            if skill_name == "start":
                return {"status": "success", "data": {"approved": True}}
            return {"status": "success", "data": {"branch": skill_name}}

    result = await GraphExecutor(state_manager, BranchRunner()).run(
        graph,
        GraphRun(run_id="run-branch", graph_id=graph.graph_id, status="pending"),
        make_context(),
    )

    assert result.status == "done"
    assert graph.nodes["approved"].status == "done"
    assert graph.nodes["rejected"].status == "done"
    assert graph.nodes["rejected"].output["status"] == "skipped"
    assert "branch" not in graph.nodes["rejected"].output.get("data", {})


def test_state_manager_roundtrip() -> None:
    state_manager = StateManager(FakeConnection())
    run = GraphRun(run_id="run-1", graph_id="graph-1", status="running", state={"count": 1})
    node = Node(
        id="step1",
        skill="echo_skill",
        status="done",
        input={"count": 1},
        output={"status": "success", "data": {"count": 1}},
    )

    state_manager.save_run(run)
    state_manager.save_node(run.run_id, node)

    loaded_run = state_manager.load_run("run-1")
    loaded_nodes = state_manager.load_nodes("run-1")

    assert loaded_run is not None
    assert loaded_run.graph_id == "graph-1"
    assert loaded_run.state == {"count": 1}
    assert loaded_run.node_results["step1"] == {"status": "success", "data": {"count": 1}}
    assert loaded_nodes["step1"].status == "done"
    assert loaded_nodes["step1"].output == {"status": "success", "data": {"count": 1}}


def test_state_manager_uses_psycopg_for_postgresql_uri(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakePsycopgModule:
        @staticmethod
        def connect(uri, autocommit=True):
            captured["uri"] = uri
            captured["autocommit"] = autocommit
            return FakeConnection()

    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopgModule())

    state_manager = StateManager("postgresql://user:pass@localhost:5432/ithqbot")

    assert isinstance(state_manager.db, FakeConnection)
    assert captured == {
        "uri": "postgresql://user:pass@localhost:5432/ithqbot",
        "autocommit": True,
    }


@pytest.mark.asyncio
async def test_graph_executor_runs_ready_nodes_in_parallel() -> None:
    graph = load_graph_data(
        {
            "graph_id": "parallel_flow",
            "nodes": [
                {"id": "left", "skill": "left", "input": {"seed": "L"}},
                {"id": "right", "skill": "right", "input": {"seed": "R"}},
                {"id": "join", "skill": "join"},
            ],
            "edges": [
                {"from": "left", "to": "join"},
                {"from": "right", "to": "join"},
            ],
        }
    )
    run = GraphRun(run_id="run-parallel", graph_id=graph.graph_id, status="pending")
    state_manager = StateManager(FakeConnection())
    runner = ConcurrentRunner()
    executor = GraphExecutor(state_manager, runner)

    result = await executor.run(graph, run, make_context())

    assert result.status == "done"
    assert runner.max_active >= 2
    assert graph.nodes["join"].status == "done"
    assert result.state["left"] == "L"
    assert result.state["right"] == "R"
    assert result.state["join"] == "join"


@pytest.mark.asyncio
async def test_graph_executor_interrupts_and_resume_restores_waiting_run() -> None:
    graph_data = {
        "graph_id": "resume_flow",
        "nodes": [
            {"id": "ask", "skill": "ask_user"},
            {"id": "finish", "skill": "finish"},
        ],
        "edges": [{"from": "ask", "to": "finish"}],
    }
    graph = load_graph_data(graph_data)
    state_manager = StateManager(FakeConnection())
    runner = InteractionRunner()
    executor = GraphExecutor(state_manager, runner)
    run = GraphRun(run_id="run-resume", graph_id=graph.graph_id, status="pending")

    initial_run = await executor.run(graph, run, make_context())

    persisted_run = state_manager.load_run(run.run_id)
    persisted_nodes = state_manager.load_nodes(run.run_id)

    assert initial_run.status == "waiting"
    assert persisted_run is not None
    assert persisted_run.status == "waiting"
    assert persisted_nodes["ask"].status == "waiting"

    resumed_graph = load_graph_data(graph_data)
    resumed_run = await executor.resume(
        run.run_id,
        {"answer": "ok"},
        make_context(),
        graph=resumed_graph,
    )

    assert resumed_run.status == "done"
    assert resumed_run.state["answer"] == "ok"
    assert resumed_run.state["final_answer"] == "ok"
    assert resumed_graph.nodes["ask"].status == "done"
    assert resumed_graph.nodes["finish"].status == "done"
    assert any(call[2] == "ask_user" for call in runner.calls)


@pytest.mark.asyncio
async def test_graph_executor_preserves_skill_context_fields() -> None:
    graph = load_graph_data(
        {
            "graph_id": "context_flow",
            "nodes": [{"id": "step1", "skill": "echo_skill"}],
            "edges": [],
        }
    )
    runner = ContextCapturingRunner()
    executor = GraphExecutor(StateManager(FakeConnection()), runner)

    await executor.run(
        graph,
        GraphRun(run_id="run-context", graph_id=graph.graph_id, status="pending"),
        make_context(),
    )

    assert runner.contexts == [
        {
            "skill_name": "echo_skill",
            "tenant_id": "tenant",
            "account_id": "account",
            "chat_id": "chat",
            "bot_id": "bot",
            "parent_run_id": "run-context",
            "metadata": {
                "source": "test",
                "graph": {
                    "graph_id": "context_flow",
                    "run_id": "run-context",
                    "node_id": "step1",
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_graph_executor_emits_status_updates_and_trace_events() -> None:
    graph = load_graph_data(
        {
            "graph_id": "trace_flow",
            "nodes": [
                {"id": "step1", "skill": "first"},
                {"id": "step2", "skill": "second"},
            ],
            "edges": [{"from": "step1", "to": "step2"}],
        }
    )

    class TraceRunner:
        async def run(self, skill_name, payload, context):
            return {"status": "success", "data": {skill_name: "ok"}}

    progress_events = []

    async def emit_callback(message: str, **kwargs):
        progress_events.append({"message": message, **kwargs})

    request_msg_id = f"graph-{uuid.uuid4().hex}"
    context_obj = make_trace_context(emit_callback=emit_callback, request_msg_id=request_msg_id)
    result = await GraphExecutor(StateManager(FakeConnection()), TraceRunner()).run(
        graph,
        GraphRun(run_id="run-trace", graph_id=graph.graph_id, status="pending"),
        context_obj,
    )

    assert result.status == "done"
    assert progress_events[0]["progress_stage"] == "graph_running"
    assert progress_events[0]["status_details"]["graph"]["run_id"] == "run-trace"
    assert any(event["message"] == "开始执行节点 step1" for event in progress_events)
    assert progress_events[-1]["message"] == "技能图执行完成"
    assert progress_events[-1]["status_details"]["graph"]["completed_nodes"] == 2

    trace_events = get_trace_task_events(request_msg_id, limit=20)
    event_names = [event["event_name"] for event in trace_events]
    assert "graph.run.start" in event_names
    assert "graph.node.start" in event_names
    assert "graph.node.done" in event_names
    assert "graph.run.done" in event_names
    completed_event = next(event for event in trace_events if event["event_name"] == "graph.node.done")
    assert completed_event["duration_ms"] is not None
    assert completed_event["parent_run_id"] == "run-trace"
    assert completed_event["run_id"] in {"run-trace:step1", "run-trace:step2"}
    assert completed_event["details"]["graph"]["run_id"] == "run-trace"
    assert completed_event["details"]["node"]["node_id"] in {"step1", "step2"}

    trace_tree = get_trace_tree(request_msg_id)
    assert trace_tree is not None
    graph_run = next(node for node in trace_tree["tree"] if node["id"] == "run-trace")
    child_ids = {child["id"] for child in graph_run["children"]}
    assert child_ids == {"run-trace:step1", "run-trace:step2"}
    assert all(child["parent_id"] == "run-trace" for child in graph_run["children"])


@pytest.mark.asyncio
async def test_graph_executor_supports_input_and_output_mapping() -> None:
    graph = load_graph_from_dict(
        {
            "graph_id": "mapping_flow",
            "nodes": [
                {
                    "id": "fetch",
                    "skill": "fetch_alarm",
                    "output_mapping": {"alarm_id": "data.alarm.id"},
                },
                {
                    "id": "analyze",
                    "skill": "analyze_alarm",
                    "input_mapping": {"alarm_id": "state.alarm_id"},
                    "output_mapping": {"summary": "data.summary"},
                },
            ],
            "edges": [{"from": "fetch", "to": "analyze"}],
        }
    )

    class MappingRunner:
        def __init__(self):
            self.payloads = []

        async def run(self, skill_name, payload, context):
            self.payloads.append((skill_name, dict(payload)))
            if skill_name == "fetch_alarm":
                return {"status": "success", "data": {"alarm": {"id": "A-1"}}}
            return {"status": "success", "data": {"summary": f"alarm={payload['alarm_id']}"}}

    runner = MappingRunner()
    result = await GraphExecutor(StateManager(FakeConnection()), runner).run(
        graph,
        GraphRun(run_id="run-mapping", graph_id=graph.graph_id, status="pending"),
        make_context(),
    )

    assert result.status == "done"
    assert result.state["alarm_id"] == "A-1"
    assert result.state["summary"] == "alarm=A-1"
    assert runner.payloads[1][1]["alarm_id"] == "A-1"


@pytest.mark.asyncio
async def test_graph_executor_keeps_parallel_nodes_running_when_one_waits_for_input() -> None:
    graph = load_graph_from_dict(
        {
            "graph_id": "parallel_wait_flow",
            "nodes": [
                {"id": "start", "skill": "seed"},
                {"id": "ask", "skill": "ask_user"},
                {
                    "id": "fast",
                    "skill": "fast_path",
                    "output_mapping": {"fast_result": "data.fast_result"},
                },
            ],
            "edges": [
                {"from": "start", "to": "ask"},
                {"from": "start", "to": "fast"},
            ],
        }
    )

    class ParallelWaitingRunner:
        async def run(self, skill_name, payload, context):
            if skill_name == "seed":
                return {"status": "success", "data": {"seed": "ok"}}
            if skill_name == "ask_user":
                raise InteractionRequired(
                    {
                        "type": "form",
                        "title": "补充信息",
                        "prompt": "请输入 answer",
                        "fields": [{"key": "answer", "label": "answer", "input_type": "text"}],
                    }
                )
            if skill_name == "fast_path":
                await asyncio.sleep(0.05)
                return {"status": "success", "data": {"fast_result": "done"}}
            raise AssertionError(f"unexpected skill: {skill_name}")

    run = await GraphExecutor(StateManager(FakeConnection()), ParallelWaitingRunner()).run(
        graph,
        GraphRun(run_id="run-parallel-wait", graph_id=graph.graph_id, status="pending"),
        make_context(),
    )

    assert run.status == "waiting"
    assert graph.nodes["ask"].status == "waiting"
    assert graph.nodes["fast"].status == "done"
    assert run.state["fast_result"] == "done"


def test_load_graph_by_id_finds_yaml_in_workspace_graphs(tmp_path: Path) -> None:
    graphs_dir = tmp_path / "graphs"
    graphs_dir.mkdir()
    graph_file = graphs_dir / "demo.yaml"
    graph_file.write_text(
        """
graph_id: demo
nodes:
  - id: step1
    skill: echo
edges: []
""".strip(),
        encoding="utf-8",
    )

    graph = load_graph_by_id("demo", [graphs_dir])

    assert graph.graph_id == "demo"


def test_load_graph_by_id_finds_composite_graph_in_skills_dir(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "weekly_report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "graph.yaml").write_text(
        """
graph_id: weekly_report
nodes:
  - id: step1
    skill: task_query
edges: []
""".strip(),
        encoding="utf-8",
    )

    graph = load_graph_by_id("weekly_report", [tmp_path / "skills"])

    assert graph.graph_id == "weekly_report"
    assert list(graph.nodes) == ["step1"]


def test_planner_skill_index_reads_description_and_schema(tmp_path: Path) -> None:
    builtin_dir = tmp_path / "builtin-skills"
    builtin_dir.mkdir()
    skill_dir = tmp_path / "skills" / "alarm_query"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
metadata:
  ithqbot:
    capability: ["observability", "analysis"]
    tags: ["alarm", "root-cause"]
    level: "atomic"
    idempotent: true
    retryable: true
    cost:
      level: low
    latency:
      expected_ms: 800
    planner:
      output_to: ["log_analysis"]
description: 查询告警
---

alarm query
""",
        encoding="utf-8",
    )
    (skill_dir / "schema.json").write_text(
        """
{
  "input_schema":{"type":"object","properties":{"alarm_id":{"type":"string"}},"required":["alarm_id"]},
  "output_schema":{"type":"object","properties":{"alarm_id":{"type":"string"},"logs":{"type":"array"}}},
  "semantic":{"produces":["alarm_id","logs"],"consumes":["alarm_query"]}
}
""".strip(),
        encoding="utf-8",
    )

    skills_loader = SkillsLoader(tmp_path, builtin_skills_dir=builtin_dir)
    skills = load_skills(skills_loader)

    assert skills == [
        SkillInfo(
            name="alarm_query",
            description="查询告警",
            input_schema={
                "type": "object",
                "properties": {"alarm_id": {"type": "string"}},
                "required": ["alarm_id"],
            },
            output_schema={
                "type": "object",
                "properties": {"alarm_id": {"type": "string"}, "logs": {"type": "array"}},
            },
            semantic={"produces": ["alarm_id", "logs"], "consumes": ["alarm_query"]},
            capability=["observability", "analysis"],
            tags=["alarm", "root-cause"],
            level="atomic",
            planner={"output_to": ["log_analysis"]},
            idempotent=True,
            retryable=True,
            cost={"level": "low"},
            latency={"expected_ms": 800},
        )
    ]
    assert "Skill: alarm_query" in build_skill_text(skills)
    skill_text = build_skill_text(skills)
    assert 'Output: {"type": "object"' in skill_text
    assert 'Semantic: {"produces": ["alarm_id", "logs"]' in skill_text
    assert 'Capability: ["observability", "analysis"]' in skill_text


def test_planner_skill_index_prefers_capability_contract(tmp_path: Path) -> None:
    builtin_dir = tmp_path / "builtin-skills"
    builtin_dir.mkdir()
    skill_dir = tmp_path / "skills" / "alarm_query"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
metadata:
  ithqbot:
    capability: ["observability", "analysis"]
description: 查询告警
---

alarm query
""",
        encoding="utf-8",
    )
    (skill_dir / "capability.json").write_text(
        """
{
  "name": "observability.alarm_query",
  "description": "Capability contract",
  "version": "1.0.0",
  "namespace": "observability",
  "category": "atomic",
  "input_schema": {
    "type": "object",
    "properties": {
      "alarm_id": {"type": "string"}
    },
    "required": ["alarm_id"]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "logs": {"type": "array"}
    }
  },
  "semantic": {
    "produces": [{"name": "logs"}],
    "consumes": [{"name": "alarm_query"}]
  },
  "idempotent": true,
  "retryable": true,
  "cost": {
    "level": "low"
  },
  "sla": {
    "latency_ms": 900,
    "availability": "best_effort"
  }
}
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "schema.json").write_text(
        """
{
  "input_schema":{"type":"object","properties":{"legacy":{"type":"string"}}},
  "output_schema":{"type":"object","properties":{"legacy":{"type":"string"}}},
  "semantic":{"produces":["legacy_output"],"consumes":["legacy_input"]}
}
""".strip(),
        encoding="utf-8",
    )

    skills_loader = SkillsLoader(tmp_path, builtin_skills_dir=builtin_dir)
    skills = load_skills(skills_loader)

    assert skills == [
        SkillInfo(
            name="alarm_query",
            description="查询告警",
            input_schema={
                "type": "object",
                "properties": {"alarm_id": {"type": "string"}},
                "required": ["alarm_id"],
            },
            output_schema={
                "type": "object",
                "properties": {"logs": {"type": "array"}},
            },
            semantic={"produces": ["logs"], "consumes": ["alarm_query"]},
            capability=["observability", "analysis"],
            tags=[],
            level="atomic",
            planner={},
            idempotent=True,
            retryable=True,
            cost={"level": "low"},
            latency={"expected_ms": 900, "availability": "best_effort"},
        )
    ]
    skill_text = build_skill_text(skills)
    assert "Skill: alarm_query" in skill_text
    assert 'Planner: {}' in skill_text
    assert "Idempotent: true" in skill_text
    assert 'Cost: {"level": "low"}' in skill_text


@pytest.mark.asyncio
async def test_graph_planner_safe_plan_retries_and_returns_valid_graph() -> None:
    responses = iter(
        [
            "not-json",
            """```json
{"graph_id":"fault_analysis","nodes":[{"id":"step1","skill":"alarm_query"},{"id":"step2","skill":"log_analysis"}],"edges":[{"from":"step1","to":"step2"}]}
```""",
        ]
    )

    async def fake_llm(_prompt: str) -> str:
        return next(responses)

    class StubSkillLoader:
        def load_skills(self):
            return [
                SkillInfo(name="alarm_query", description="查询告警", input_schema={}),
                SkillInfo(name="log_analysis", description="分析日志", input_schema={}),
            ]

    planner = GraphPlanner(fake_llm, StubSkillLoader())
    graph = await safe_plan(planner, "分析告警并给出根因")

    assert graph["graph_id"] == "fault_analysis"
    assert [node["skill"] for node in graph["nodes"]] == ["alarm_query", "log_analysis"]


@pytest.mark.asyncio
async def test_graph_planner_records_retry_and_completion_trace_events() -> None:
    responses = iter(
        [
            "not-json",
            """```json
{"graph_id":"fault_analysis","nodes":[{"id":"step1","skill":"alarm_query"}],"edges":[]}
```""",
        ]
    )

    async def fake_llm(_prompt: str) -> str:
        return next(responses)

    class StubSkillLoader:
        def load_skills(self):
            return [SkillInfo(name="alarm_query", description="查询告警", input_schema={})]

    request_msg_id = f"planner-{uuid.uuid4().hex}"
    tokens = context.set_runtime_context(
        account="account",
        tenant="tenant",
        bot="bot",
        channel_name="icatmsg",
        chat="chat",
        metadata={"request_msg_id": request_msg_id, "trace_id": f"trace-{request_msg_id}", "client_id": "client"},
    )
    try:
        graph = await safe_plan(GraphPlanner(fake_llm, StubSkillLoader()), "分析告警并给出根因")
    finally:
        context.reset_runtime_context(tokens)

    assert graph["graph_id"] == "fault_analysis"
    trace_events = get_trace_task_events(request_msg_id, limit=20)
    event_names = [event["event_name"] for event in trace_events]
    assert event_names.count("graph.planner.started") == 2
    assert "graph.planner.failed" in event_names
    assert "graph.planner.retry" in event_names
    assert "graph.planner.completed" in event_names
    completed_event = next(event for event in trace_events if event["event_name"] == "graph.planner.completed")
    assert completed_event["duration_ms"] is not None
    assert completed_event["run_id"] == f"planner:trace-{request_msg_id}:2"
    assert completed_event["details"]["planner_attempt"] == 2
    assert completed_event["details"]["graph_id"] == "fault_analysis"
    retry_event = next(event for event in trace_events if event["event_name"] == "graph.planner.retry")
    assert retry_event["parent_run_id"] == f"planner:trace-{request_msg_id}:1"


@pytest.mark.asyncio
async def test_graph_planner_prompt_includes_conditional_few_shot_example() -> None:
    captured_prompt: dict[str, str] = {}

    async def fake_llm(prompt: str) -> str:
        captured_prompt["value"] = prompt
        return (
            '{"graph_id":"approval_flow","nodes":['
            '{"id":"step1","skill":"approval_check"},'
            '{"id":"step2","skill":"approve_action"},'
            '{"id":"step3","skill":"reject_action"},'
            '{"id":"step4","skill":"result_summary"}'
            '],"edges":['
            '{"from":"step1","to":"step2","condition":"state.approved == true"},'
            '{"from":"step1","to":"step3","condition":"state.approved == false"},'
            '{"from":"step2","to":"step4"},'
            '{"from":"step3","to":"step4"}'
            ']}'
        )

    class StubSkillLoader:
        def load_skills(self):
            return [
                SkillInfo(name="approval_check", description="审批检查", input_schema={}),
                SkillInfo(name="approve_action", description="通过处理", input_schema={}),
                SkillInfo(name="reject_action", description="拒绝处理", input_schema={}),
                SkillInfo(name="result_summary", description="结果汇总", input_schema={}),
            ]

    planner = GraphPlanner(fake_llm, StubSkillLoader())
    graph = await safe_plan(planner, "根据审批结果选择执行路径并汇总结果")

    assert graph["graph_id"] == "approval_flow"
    assert "根据审批结果选择执行路径并汇总结果" in EXAMPLES
    prompt = captured_prompt["value"]
    assert "根据审批结果选择执行路径并汇总结果" in prompt
    assert '"condition": "state.approved == true"' in prompt
    assert build_prompt("x", "y", EXAMPLES).count("用户需求：") >= 3


@pytest.mark.asyncio
async def test_graph_planner_retry_prompt_includes_previous_error() -> None:
    captured_prompts: list[str] = []
    responses = iter(
        [
            "not-json",
            '{"graph_id":"fault_analysis","nodes":[{"id":"step1","skill":"alarm_query"}],"edges":[]}',
        ]
    )

    async def fake_llm(prompt: str) -> str:
        captured_prompts.append(prompt)
        return next(responses)

    class StubSkillLoader:
        def load_skills(self):
            return [SkillInfo(name="alarm_query", description="查询告警", input_schema={})]

    graph = await safe_plan(GraphPlanner(fake_llm, StubSkillLoader()), "分析告警并给出根因")

    assert graph["graph_id"] == "fault_analysis"
    assert len(captured_prompts) == 2
    assert "上一轮失败原因" in captured_prompts[1]
    assert "Invalid JSON from LLM" in captured_prompts[1]


def test_validate_graph_rejects_semantic_mismatch() -> None:
    graph = {
        "graph_id": "semantic_mismatch_flow",
        "nodes": [
            {"id": "step1", "skill": "meeting_summary"},
            {"id": "step2", "skill": "task_generate"},
        ],
        "edges": [{"from": "step1", "to": "step2"}],
    }
    skills = [
        SkillInfo(name="meeting_summary", semantic={"produces": ["meeting_summary"]}),
        SkillInfo(name="task_generate", semantic={"consumes": ["task_list"]}),
    ]

    with pytest.raises(ValueError, match="Semantic mismatch"):
        validate_graph(graph, skills)


def test_validate_graph_enforces_planner_hints() -> None:
    graph = {
        "graph_id": "planner_hints_flow",
        "nodes": [
            {"id": "step1", "skill": "document_parse"},
            {"id": "step2", "skill": "task_generate"},
        ],
        "edges": [{"from": "step1", "to": "step2"}],
    }
    skills = [
        SkillInfo(name="document_parse"),
        SkillInfo(name="task_generate", planner={"input_from": ["meeting_summary"]}),
    ]

    with pytest.raises(ValueError, match="Planner constraint violation"):
        validate_graph(graph, skills)


@pytest.mark.asyncio
async def test_agent_loop_run_graph_by_id_uses_existing_tool_chain(tmp_path: Path) -> None:
    graphs_dir = tmp_path / "graphs"
    graphs_dir.mkdir()
    (graphs_dir / "echo_flow.yaml").write_text(
        """
graph_id: echo_flow
nodes:
  - id: step1
    skill: graph_echo
edges: []
""".strip(),
        encoding="utf-8",
    )

    class GraphEchoTool(Tool):
        @property
        def name(self) -> str:
            return "graph_echo"

        @property
        def description(self) -> str:
            return "echo graph payload"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {"seed": {"type": "string"}},
            }

        async def execute(self, **kwargs):
            context = kwargs.get("context")
            return (
                '{"status":"success","data":{"echo":"'
                + str(kwargs.get("seed", "ok"))
                + '","skill_name":"'
                + str(context.skill_name)
                + '"}}'
            )

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(GraphEchoTool())

    run = await loop.run_graph_by_id(
        "echo_flow",
        metadata={"tenant_id": "tenant", "account_id": "account", "chat_id": "chat"},
        initial_state={"seed": "hello"},
    )

    assert run.status == "done"
    assert run.state["echo"] == "hello"
    assert run.state["skill_name"] == "graph_echo"


@pytest.mark.asyncio
async def test_agent_loop_handle_request_via_graph_uses_planner(tmp_path: Path) -> None:
    class GraphEchoTool(Tool):
        @property
        def name(self) -> str:
            return "graph_echo"

        @property
        def description(self) -> str:
            return "echo graph payload"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {"seed": {"type": "string"}},
            }

        async def execute(self, **kwargs):
            return '{"status":"success","data":{"echo":"planned"}}'

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content='{"graph_id":"planned_flow","nodes":[{"id":"step1","skill":"graph_echo"}],"edges":[]}',
            tool_calls=[],
        )
    )
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(GraphEchoTool())

    run = await loop.handle_request_via_graph(
        "帮我执行一个回显图",
        metadata={"tenant_id": "tenant", "account_id": "account", "chat_id": "chat"},
        initial_state={"seed": "ignored"},
    )

    assert run.status == "done"
    assert run.graph_id == "planned_flow"
    assert run.state["echo"] == "planned"
    provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_loop_handle_request_via_graph_routes_skill_compliance_to_check_skill(tmp_path: Path) -> None:
    class DocCompareTool(Tool):
        @property
        def name(self) -> str:
            return "doc_compare"

        @property
        def description(self) -> str:
            return "compare two documents"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {"path1": {"type": "string"}, "path2": {"type": "string"}}}

        async def execute(self, **kwargs):
            return '{"status":"success","data":{"summary":"doc-compare"}}'

    class CheckSkillTool(Tool):
        @property
        def name(self) -> str:
            return "check_skill"

        @property
        def description(self) -> str:
            return "check skill compliance"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {"skill_name": {"type": "string"}, "strict": {"type": "boolean"}}}

        async def execute(self, **kwargs):
            return (
                '{"status":"success","data":{"summary":"checked:'
                + str(kwargs.get("skill_name", ""))
                + '"}}'
            )

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    provider.chat_with_retry = AsyncMock()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(DocCompareTool())
    loop.tools.register(CheckSkillTool())

    run = await loop.handle_request_via_graph(
        "帮我检查一下 doc_compare skill 是否符合 SKILL_STANDARDS.md 的规范",
        metadata={"tenant_id": "tenant", "account_id": "account", "chat_id": "chat"},
    )

    assert run.status == "done"
    assert run.graph_id == "skill_compliance_check"
    assert run.state["summary"] == "checked:doc_compare"
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_loop_handle_request_via_graph_executes_conditional_merge_flow(tmp_path: Path) -> None:
    class ApprovalCheckTool(Tool):
        @property
        def name(self) -> str:
            return "approval_check"

        @property
        def description(self) -> str:
            return "check approval"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return '{"status":"success","data":{"approved":true,"request_id":"REQ-1"}}'

    class ApproveActionTool(Tool):
        @property
        def name(self) -> str:
            return "approve_action"

        @property
        def description(self) -> str:
            return "handle approved request"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {"request_id": {"type": "string"}}}

        async def execute(self, **kwargs):
            return '{"status":"success","data":{"message":"approved:REQ-1"}}'

    class RejectActionTool(Tool):
        @property
        def name(self) -> str:
            return "reject_action"

        @property
        def description(self) -> str:
            return "handle rejected request"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {"request_id": {"type": "string"}}}

        async def execute(self, **kwargs):
            raise AssertionError("reject branch should be skipped")

    class ResultSummaryTool(Tool):
        @property
        def name(self) -> str:
            return "result_summary"

        @property
        def description(self) -> str:
            return "summarize branch result"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                    "approved": {"type": "boolean"},
                },
            }

        async def execute(self, **kwargs):
            result = kwargs.get("result", "")
            approved = kwargs.get("approved")
            return (
                '{"status":"success","data":{"final_answer":"'
                + f"approved={approved};result={result}"
                + '"}}'
            )

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="""
{"graph_id":"approval_flow","nodes":[
  {"id":"step1","skill":"approval_check"},
  {"id":"step2","skill":"approve_action","input_mapping":{"request_id":"state.request_id"},"output_mapping":{"branch_result":"data.message"}},
  {"id":"step3","skill":"reject_action","input_mapping":{"request_id":"state.request_id"},"output_mapping":{"branch_result":"data.message"}},
  {"id":"step4","skill":"result_summary","input_mapping":{"result":"state.branch_result","approved":"state.approved"},"output_mapping":{"final_answer":"data.final_answer"}}
],"edges":[
  {"from":"step1","to":"step2","condition":"state.approved == true"},
  {"from":"step1","to":"step3","condition":"state.approved == false"},
  {"from":"step2","to":"step4"},
  {"from":"step3","to":"step4"}
]}
""".strip(),
            tool_calls=[],
        )
    )
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(ApprovalCheckTool())
    loop.tools.register(ApproveActionTool())
    loop.tools.register(RejectActionTool())
    loop.tools.register(ResultSummaryTool())

    run = await loop.handle_request_via_graph(
        "根据审批结果选择执行路径并汇总结果",
        metadata={"tenant_id": "tenant", "account_id": "account", "chat_id": "chat"},
    )

    assert run.status == "done"
    assert run.graph_id == "approval_flow"
    assert run.state["approved"] is True
    assert run.state["request_id"] == "REQ-1"
    assert run.state["branch_result"] == "approved:REQ-1"
    assert run.state["final_answer"] == "approved=True;result=approved:REQ-1"
    provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_loop_process_direct_supports_graph_slash_run(tmp_path: Path) -> None:
    graphs_dir = tmp_path / "graphs"
    graphs_dir.mkdir()
    (graphs_dir / "echo_flow.yaml").write_text(
        """
graph_id: echo_flow
nodes:
  - id: step1
    skill: graph_echo
edges: []
""".strip(),
        encoding="utf-8",
    )

    class GraphEchoTool(Tool):
        @property
        def name(self) -> str:
            return "graph_echo"

        @property
        def description(self) -> str:
            return "echo graph payload"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {"seed": {"type": "string"}}}

        async def execute(self, **kwargs):
            return '{"status":"success","data":{"echo":"hello-from-graph"}}'

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(GraphEchoTool())

    response = await loop.process_direct(
        "/graph run echo_flow",
        metadata={"graph_initial_state": {"seed": "hello"}},
    )

    assert "技能图执行完成" in response
    assert "graph_id: echo_flow" in response
    assert "hello-from-graph" in response


@pytest.mark.asyncio
async def test_agent_loop_process_direct_supports_graph_preview(tmp_path: Path) -> None:
    class GraphEchoTool(Tool):
        @property
        def name(self) -> str:
            return "graph_echo"

        @property
        def description(self) -> str:
            return "echo graph payload"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return '{"status":"success","data":{"echo":"preview"}}'

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content='{"graph_id":"preview_flow","nodes":[{"id":"step1","skill":"graph_echo"}],"edges":[]}',
            tool_calls=[],
        )
    )
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(GraphEchoTool())

    response = await loop.process_direct("/graph preview 帮我生成一个回显图")

    assert '"graph_id": "preview_flow"' in response
    assert '"skill": "graph_echo"' in response
    provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_loop_process_direct_recognizes_natural_language_graph_plan(tmp_path: Path) -> None:
    class GraphEchoTool(Tool):
        @property
        def name(self) -> str:
            return "graph_echo"

        @property
        def description(self) -> str:
            return "echo graph payload"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return '{"status":"success","data":{"summary":"planner-routed"}}'

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content='{"graph_id":"planned_flow","nodes":[{"id":"step1","skill":"graph_echo"}],"edges":[]}',
            tool_calls=[],
        )
    )
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.register(GraphEchoTool())

    response = await loop.process_direct("请使用技能图执行：帮我做一个回显任务")

    assert "技能图执行完成" in response
    assert "graph_id: planned_flow" in response
    assert "planner-routed" in response
    provider.chat_with_retry.assert_awaited_once()
