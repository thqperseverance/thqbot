from __future__ import annotations

import time
from typing import Any

from ithqbot.context import get_runtime_context
from ithqbot.graph import GraphRun, load_graph_from_dict
from ithqbot.observability import record_trace_event
from ithqbot.providers.base import LLMResponse

from .examples import EXAMPLES
from .parser import parse_output
from .prompt_builder import build_prompt
from .skill_index import build_skill_text, load_skills
from .validator import validate_graph


class GraphPlanner:
    def __init__(self, llm: Any, skill_loader: Any):
        self.llm = llm
        self.skill_loader = skill_loader

    async def plan(
        self,
        user_query: str,
        repair_hint: str | None = None,
        *,
        attempt: int = 1,
    ) -> dict[str, Any]:
        trace_context = _planner_trace_context()
        base_trace_context = _planner_event_context(trace_context)
        started_at = time.monotonic()
        planner_run_id = _planner_run_id(trace_context, attempt)
        record_trace_event(
            event_name="graph.planner.started",
            phase="start",
            status="running",
            component="graph_planner",
            source="bot",
            event_type="graph",
            details={
                "run_id": planner_run_id,
                "planner_attempt": attempt,
                "query": user_query[:500],
            },
            parent_run_id=trace_context.get("parent_run_id"),
            **base_trace_context,
        )
        try:
            skills = load_skills(self.skill_loader)
            skills_text = build_skill_text(skills)
            prompt = build_prompt(user_query, skills_text, EXAMPLES, repair_hint=repair_hint)
            response = await self._call_llm(prompt)
            graph = parse_output(response)
            validate_graph(graph, skills)
        except Exception as exc:
            record_trace_event(
                event_name="graph.planner.failed",
                phase="end",
                status="error",
                component="graph_planner",
                source="bot",
                event_type="graph",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                details={
                    "run_id": planner_run_id,
                    "planner_attempt": attempt,
                    "query": user_query[:500],
                    "error": str(exc),
                },
                parent_run_id=trace_context.get("parent_run_id"),
                **base_trace_context,
            )
            raise
        record_trace_event(
            event_name="graph.planner.completed",
            phase="end",
            status="ok",
            component="graph_planner",
            source="bot",
            event_type="graph",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            details={
                "run_id": planner_run_id,
                "planner_attempt": attempt,
                "graph_id": graph.get("graph_id"),
                "node_count": len(graph.get("nodes", [])),
                "edge_count": len(graph.get("edges", [])),
            },
            parent_run_id=trace_context.get("parent_run_id"),
            **base_trace_context,
        )
        return graph

    async def plan_as_graph(self, user_query: str):
        graph_dict = await self.plan(user_query)
        return load_graph_from_dict(graph_dict)

    async def handle_request(
        self,
        query: str,
        executor: Any,
        context: Any,
        run_id: str,
    ) -> GraphRun:
        graph = await self.plan(query)
        graph_obj = load_graph_from_dict(graph)
        run = GraphRun(run_id=run_id, graph_id=graph["graph_id"], status="pending")
        return await executor.run(graph_obj, run, context)

    async def _call_llm(self, prompt: str) -> str:
        llm = self.llm
        if callable(llm):
            response = await llm(prompt)
            return _coerce_llm_text(response)

        chat_with_retry = getattr(llm, "chat_with_retry", None)
        if callable(chat_with_retry):
            response = await chat_with_retry(messages=[{"role": "user", "content": prompt}])
            return _coerce_llm_text(response)

        raise TypeError("Planner llm must be an async callable or provider with chat_with_retry()")


async def safe_plan(planner: GraphPlanner, query: str) -> dict[str, Any]:
    last_error: Exception | None = None
    trace_context = _planner_trace_context()
    base_trace_context = _planner_event_context(trace_context)
    repair_hint: str | None = None
    for attempt in range(1, 4):
        try:
            return await planner.plan(query, repair_hint=repair_hint, attempt=attempt)
        except Exception as exc:
            last_error = exc
            repair_hint = str(exc)
            if attempt < 3:
                planner_run_id = _planner_run_id(trace_context, attempt)
                record_trace_event(
                    event_name="graph.planner.retry",
                    phase="point",
                    status="retrying",
                    component="graph_planner",
                    source="bot",
                    event_type="graph",
                    details={
                        "run_id": planner_run_id,
                        "planner_attempt": attempt,
                        "attempt": attempt,
                        "max_attempts": 3,
                        "error": str(exc),
                    },
                    parent_run_id=planner_run_id,
                    **base_trace_context,
                )
    raise RuntimeError("Planner failed") from last_error


def _coerce_llm_text(response: Any) -> str:
    if isinstance(response, LLMResponse):
        return response.content or ""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    raise TypeError("Unsupported planner LLM response type")


def _planner_trace_context() -> dict[str, Any]:
    runtime_context = get_runtime_context()
    return {
        "account_id": runtime_context.get("account_id"),
        "tenant_id": runtime_context.get("tenant_id"),
        "bot_id": runtime_context.get("bot_id"),
        "channel": runtime_context.get("channel"),
        "chat_id": runtime_context.get("chat_id"),
        "client_id": runtime_context.get("client_id"),
        "request_msg_id": runtime_context.get("request_msg_id"),
        "trace_id": runtime_context.get("trace_id"),
        "parent_run_id": runtime_context.get("parent_run_id"),
    }


def _planner_event_context(trace_context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in trace_context.items() if key != "parent_run_id"}


def _planner_run_id(trace_context: dict[str, Any], attempt: int) -> str:
    base_id = str(trace_context.get("trace_id") or trace_context.get("request_msg_id") or "planner").strip()
    return f"planner:{base_id}:{attempt}"
