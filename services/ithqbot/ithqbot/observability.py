from __future__ import annotations

import asyncio
import json
import os
import time
from threading import Lock
from typing import TYPE_CHECKING, Any, Iterable
import random

import redis

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

from loguru import logger

from ithqbot import context

if TYPE_CHECKING:
    from ithqbot.config.schema import ObservabilityConfig

_SOURCES = {"bot", "skill", "tool", "mcp"}
_TRACE_SUCCESS_EVENTS = {"agent.task.completed", "server.client.delivered"}
_TRACE_FAILURE_PHASES = {"error", "failed"}
_TRACE_MAX_LIMIT = 200
_DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS = 0.2
_DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS = 0.2


def _safe_key_part(value: str | None, default: str = "default") -> str:
    raw = (value or "").strip() or default
    return raw.replace(":", "_")


def _compact(value: Any, max_chars: int = 8000) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        if len(text) <= max_chars:
            return value
        return text[:max_chars] + "...(truncated)"
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    if len(text) <= max_chars:
        return value
    return text[:max_chars] + "...(truncated)"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short_text(value: Any, max_chars: int = 240) -> str:
    compacted = _compact(value, max_chars=max_chars)
    if compacted is None:
        return ""
    if isinstance(compacted, str):
        return compacted
    try:
        return json.dumps(compacted, ensure_ascii=False)[:max_chars]
    except Exception:
        return str(compacted)[:max_chars]


def _normalize_limit(limit: int, *, upper: int = _TRACE_MAX_LIMIT) -> int:
    return max(1, min(int(limit or 1), upper))


def _redis_connect_timeout_seconds() -> float:
    raw = os.getenv("ITHQBOT_OBSERVABILITY_REDIS_CONNECT_TIMEOUT_SECONDS", "").strip()
    try:
        return max(0.05, float(raw)) if raw else _DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS
    except Exception:
        return _DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS


def _redis_socket_timeout_seconds() -> float:
    raw = os.getenv("ITHQBOT_OBSERVABILITY_REDIS_SOCKET_TIMEOUT_SECONDS", "").strip()
    try:
        return max(0.05, float(raw)) if raw else _DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS
    except Exception:
        return _DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _normalize_text_filter(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def _match_filter(actual: Any, expected: str | None) -> bool:
    if expected is None:
        return True
    return str(actual or "").strip().lower() == expected


def _build_trace_search_text(summary: dict[str, Any]) -> str:
    parts = [
        summary.get("request_msg_id"),
        summary.get("trace_id"),
        summary.get("account_id"),
        summary.get("bot_id"),
        summary.get("channel"),
        summary.get("chat_id"),
        summary.get("client_id"),
        summary.get("status"),
        summary.get("last_event"),
        summary.get("last_component"),
        summary.get("graph_id"),
        summary.get("run_id"),
        summary.get("node_id"),
        summary.get("planner_attempt"),
        summary.get("idempotent"),
        summary.get("retryable"),
        summary.get("content_preview"),
    ]
    return " ".join(str(part).strip().lower() for part in parts if part)


def _as_bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _extract_trace_dimensions(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}

    status_details = details.get("status_details")
    status_graph = status_details.get("graph") if isinstance(status_details, dict) and isinstance(status_details.get("graph"), dict) else {}
    graph_details = details.get("graph") if isinstance(details.get("graph"), dict) else {}
    node_details = details.get("node") if isinstance(details.get("node"), dict) else {}

    graph_id = details.get("graph_id") or graph_details.get("graph_id") or status_graph.get("graph_id")
    run_id = details.get("run_id") or graph_details.get("run_id") or status_graph.get("run_id")
    node_id = details.get("node_id") or node_details.get("node_id") or status_graph.get("node_id")
    planner_attempt = details.get("planner_attempt")
    if planner_attempt is None:
        planner_attempt = details.get("attempt")
    retryable = details.get("retryable")
    if retryable is None:
        retryable = details.get("execution", {}).get("retryable") if isinstance(details.get("execution"), dict) else None
    idempotent = details.get("idempotent")
    if idempotent is None:
        idempotent = details.get("execution", {}).get("idempotent") if isinstance(details.get("execution"), dict) else None

    extracted: dict[str, Any] = {}
    if isinstance(graph_id, str) and graph_id.strip():
        extracted["graph_id"] = graph_id.strip()
    if isinstance(run_id, str) and run_id.strip():
        extracted["run_id"] = run_id.strip()
    if isinstance(node_id, str) and node_id.strip():
        extracted["node_id"] = node_id.strip()
    planner_attempt_num = _as_int(planner_attempt)
    if planner_attempt_num > 0:
        extracted["planner_attempt"] = planner_attempt_num
    retryable_bool = _as_bool_or_none(retryable)
    if retryable_bool is not None:
        extracted["retryable"] = retryable_bool
    idempotent_bool = _as_bool_or_none(idempotent)
    if idempotent_bool is not None:
        extracted["idempotent"] = idempotent_bool
    return extracted


def _trace_tree_event_id(event: dict[str, Any]) -> str:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    explicit_run_id = event.get("run_id") or details.get("run_id")
    if isinstance(explicit_run_id, str) and explicit_run_id.strip():
        return explicit_run_id.strip()
    tool_call_id = details.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id.strip():
        return tool_call_id.strip()
    return f"evt_{event.get('ts_ms')}_{event.get('event_name')}"


def _trace_tree_parent_id(event: dict[str, Any]) -> str | None:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    parent_id = event.get("parent_run_id") or details.get("parent_run_id")
    if isinstance(parent_id, str) and parent_id.strip():
        return parent_id.strip()
    return None


def _normalize_graph_node_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"ok", "success", "completed"}:
        return "done"
    if normalized in {"error"}:
        return "failed"
    if normalized in {"paused"}:
        return "waiting"
    return normalized or "unknown"


def _build_trace_graph_payload(task: dict[str, Any] | None, request_msg_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    base_payload = {**(task or {"request_msg_id": request_msg_id})}
    if not events:
        return {
            **base_payload,
            "graph_id": base_payload.get("graph_id"),
            "run_id": base_payload.get("run_id"),
            "status": base_payload.get("status"),
            "summary": {
                "node_count": 0,
                "completed_nodes": 0,
                "failed_nodes": 0,
                "waiting_nodes": 0,
                "running_nodes": 0,
                "pending_nodes": 0,
                "skipped_nodes": 0,
                "current_node_ids": [],
            },
            "nodes": [],
            "edges": [],
        }

    sorted_events = sorted(events, key=lambda item: int(item.get("ts_ms") or 0))
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    graph_id = str(base_payload.get("graph_id") or "").strip() or None
    run_id = str(base_payload.get("run_id") or "").strip() or None
    status = str(base_payload.get("status") or "").strip() or None
    summary: dict[str, Any] = {
        "node_count": 0,
        "completed_nodes": 0,
        "failed_nodes": 0,
        "waiting_nodes": 0,
        "running_nodes": 0,
        "pending_nodes": 0,
        "skipped_nodes": 0,
        "current_node_ids": [],
    }
    topology_order: dict[str, int] = {}

    for event in sorted_events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        graph_block = details.get("graph") if isinstance(details.get("graph"), dict) else {}
        node_block = details.get("node") if isinstance(details.get("node"), dict) else {}
        graph_id = (
            graph_id
            or str(details.get("graph_id") or graph_block.get("graph_id") or "").strip()
            or None
        )
        event_run_id = str(details.get("run_id") or graph_block.get("run_id") or "").strip()
        if event_run_id and ":" not in event_run_id:
            run_id = event_run_id
        status = str(graph_block.get("status") or event.get("status") or status or "").strip() or status
        if isinstance(graph_block.get("current_node_ids"), list):
            summary["current_node_ids"] = [
                str(item).strip()
                for item in graph_block.get("current_node_ids", [])
                if str(item).strip()
            ]

        for summary_key in (
            "node_count",
            "completed_nodes",
            "failed_nodes",
            "waiting_nodes",
            "running_nodes",
            "pending_nodes",
            "skipped_nodes",
        ):
            candidate = _as_int(graph_block.get(summary_key))
            if candidate >= 0:
                summary[summary_key] = candidate

        topology = graph_block.get("topology") if isinstance(graph_block.get("topology"), dict) else {}
        topology_nodes = topology.get("nodes") if isinstance(topology.get("nodes"), list) else []
        topology_edges = topology.get("edges") if isinstance(topology.get("edges"), list) else []
        for index, item in enumerate(topology_nodes):
            if not isinstance(item, dict):
                continue
            topology_node_id = str(item.get("node_id") or item.get("id") or "").strip()
            if not topology_node_id:
                continue
            topology_order.setdefault(topology_node_id, index)
            existing = nodes_by_id.setdefault(
                topology_node_id,
                {
                    "node_id": topology_node_id,
                    "run_id": None,
                    "parent_run_id": run_id,
                    "skill_name": str(item.get("skill_name") or item.get("skill") or "").strip() or None,
                    "status": _normalize_graph_node_status(item.get("status")),
                    "started_at": None,
                    "finished_at": None,
                    "duration_ms": None,
                    "error": None,
                    "waiting_for_input": False,
                    "interaction": None,
                    "state_update_keys": [],
                    "input_mapping_keys": sorted(str(key) for key in item.get("input_mapping_keys", []) if str(key).strip()),
                    "output_mapping_keys": sorted(str(key) for key in item.get("output_mapping_keys", []) if str(key).strip()),
                    "latest_event_name": None,
                },
            )
            if not existing.get("skill_name") and item.get("skill_name"):
                existing["skill_name"] = str(item.get("skill_name")).strip()
            if existing.get("status") in {None, "", "unknown"}:
                existing["status"] = _normalize_graph_node_status(item.get("status"))

        for item in topology_edges:
            if not isinstance(item, dict):
                continue
            from_node = str(item.get("from") or "").strip()
            to_node = str(item.get("to") or "").strip()
            if not from_node or not to_node:
                continue
            condition = str(item.get("condition") or "").strip()
            edge_key = (from_node, to_node, condition)
            if edge_key not in edges:
                edges[edge_key] = {
                    "from": from_node,
                    "to": to_node,
                    "condition": condition or None,
                    "kind": str(item.get("kind") or "dependency").strip() or "dependency",
                }

        node_id = str(details.get("node_id") or node_block.get("node_id") or "").strip()
        if not node_id:
            continue
        node_entry = nodes_by_id.setdefault(
            node_id,
            {
                "node_id": node_id,
                "run_id": None,
                "parent_run_id": _trace_tree_parent_id(event) or run_id,
                "skill_name": None,
                "status": "unknown",
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "error": None,
                "waiting_for_input": False,
                "interaction": None,
                "state_update_keys": [],
                "input_mapping_keys": [],
                "output_mapping_keys": [],
                "latest_event_name": None,
            },
        )
        node_entry["parent_run_id"] = _trace_tree_parent_id(event) or node_entry.get("parent_run_id") or run_id
        if event_run_id:
            node_entry["run_id"] = event_run_id
        skill_name = str(node_block.get("skill_name") or event.get("component") or "").strip()
        if skill_name:
            node_entry["skill_name"] = skill_name
        node_status = _normalize_graph_node_status(node_block.get("status") or event.get("status"))
        if node_status:
            node_entry["status"] = node_status
        node_entry["latest_event_name"] = str(event.get("event_name") or "").strip() or node_entry.get("latest_event_name")

        for field_name in ("input_mapping_keys", "output_mapping_keys", "state_update_keys"):
            values = node_block.get(field_name)
            if isinstance(values, list):
                node_entry[field_name] = sorted(str(item).strip() for item in values if str(item).strip())

        if isinstance(node_block.get("interaction"), dict):
            node_entry["interaction"] = node_block.get("interaction")
            node_entry["waiting_for_input"] = True
        if _as_bool_or_none(node_block.get("waiting_for_input")) is True:
            node_entry["waiting_for_input"] = True
        error_text = str(node_block.get("error") or "").strip()
        if error_text:
            node_entry["error"] = error_text

        ts_ms = _as_int(event.get("ts_ms"))
        if ts_ms > 0:
            event_name = str(event.get("event_name") or "").strip()
            phase = str(event.get("phase") or "").strip()
            if event_name.endswith(".start") or phase == "start":
                current_started_at = _as_int(node_entry.get("started_at"))
                node_entry["started_at"] = ts_ms if current_started_at <= 0 else min(current_started_at, ts_ms)
            elif node_entry.get("started_at") is None:
                node_entry["started_at"] = ts_ms

            if event_name.endswith((".done", ".failed", ".waiting", ".skipped")) or phase == "end":
                node_entry["finished_at"] = ts_ms
            duration_ms = _as_int(event.get("duration_ms"))
            if duration_ms >= 0 and event.get("duration_ms") is not None:
                node_entry["duration_ms"] = duration_ms
                if node_entry.get("started_at") is None and ts_ms > 0:
                    node_entry["started_at"] = max(0, ts_ms - duration_ms)
                if node_entry.get("finished_at") is None and ts_ms > 0:
                    node_entry["finished_at"] = ts_ms

    derived_current_node_ids = sorted(
        node_id for node_id, node in nodes_by_id.items() if node.get("status") in {"running", "waiting"}
    )
    has_topology_snapshot = bool(topology_order)
    if has_topology_snapshot:
        summary["current_node_ids"] = derived_current_node_ids
    elif not summary["current_node_ids"]:
        summary["current_node_ids"] = derived_current_node_ids

    derived_counts = {
        "completed_nodes": sum(1 for node in nodes_by_id.values() if node.get("status") == "done"),
        "failed_nodes": sum(1 for node in nodes_by_id.values() if node.get("status") == "failed"),
        "waiting_nodes": sum(1 for node in nodes_by_id.values() if node.get("status") == "waiting"),
        "running_nodes": sum(1 for node in nodes_by_id.values() if node.get("status") == "running"),
        "pending_nodes": sum(1 for node in nodes_by_id.values() if node.get("status") == "pending"),
        "skipped_nodes": sum(1 for node in nodes_by_id.values() if node.get("status") == "skipped"),
    }
    if has_topology_snapshot:
        summary["node_count"] = max(summary["node_count"], len(nodes_by_id))
        summary.update(derived_counts)
    else:
        if summary["node_count"] <= 0:
            summary["node_count"] = len(nodes_by_id)
        for key, value in derived_counts.items():
            if summary[key] <= 0:
                summary[key] = value

    normalized_status = _normalize_graph_node_status(status)
    if normalized_status == "unknown":
        if summary["failed_nodes"] > 0:
            normalized_status = "failed"
        elif summary["waiting_nodes"] > 0:
            normalized_status = "waiting"
        elif summary["running_nodes"] > 0:
            normalized_status = "running"
        elif summary["completed_nodes"] == max(summary["node_count"], 1):
            normalized_status = "done"
        else:
            normalized_status = "pending"

    ordered_nodes = sorted(
        nodes_by_id.values(),
        key=lambda item: (
            topology_order.get(str(item.get("node_id") or ""), 10_000),
            _as_int(item.get("started_at")) if item.get("started_at") is not None else 10**18,
            str(item.get("node_id") or ""),
        ),
    )

    return {
        **base_payload,
        "graph_id": graph_id,
        "run_id": run_id,
        "status": normalized_status,
        "summary": summary,
        "nodes": ordered_nodes,
        "edges": list(edges.values()),
    }


class ObservabilityPersistence:
    """Manages PostgreSQL persistence for traces and events."""

    def __init__(self, uri: str | None) -> None:
        self._uri = uri
        self._enabled = bool(uri and psycopg)

    async def _get_conn(self):
        if not self._enabled:
            return None
        try:
            return await psycopg.AsyncConnection.connect(self._uri, row_factory=dict_row)
        except Exception as exc:
            logger.warning("FAILED to connect to Observability PG: {}", exc)
            return None

    async def save_runs(self, runs: Iterable[dict[str, Any]]) -> bool:
        if not self._enabled:
            return False
        conn = await self._get_conn()
        if not conn:
            return False
        try:
            async with conn:
                async with conn.cursor() as cur:
                    for run in runs:
                        await cur.execute(
                            """
                            INSERT INTO observability_runs (
                                run_id, trace_id, parent_id, tenant_id, account_id, bot_id,
                                channel, status, event_type, last_event, last_component,
                                last_model, total_tokens, event_count, content_preview,
                                started_at, finished_at, updated_at
                            ) VALUES (
                                %(request_msg_id)s, %(trace_id)s, %(parent_run_id)s, %(tenant_id)s,
                                %(account_id)s, %(bot_id)s, %(channel)s, %(status)s, %(event_type)s,
                                %(last_event)s, %(last_component)s, %(last_model)s, %(total_tokens)s,
                                %(event_count)s, %(content_preview)s,
                                to_timestamp(%(started_at_ms)s::bigint / 1000.0),
                                to_timestamp(%(finished_at_ms)s::bigint / 1000.0),
                                to_timestamp(%(updated_at_ms)s::bigint / 1000.0)
                            ) ON CONFLICT (run_id) DO UPDATE SET
                                status = EXCLUDED.status,
                                last_event = EXCLUDED.last_event,
                                last_component = EXCLUDED.last_component,
                                last_model = EXCLUDED.last_model,
                                total_tokens = EXCLUDED.total_tokens,
                                event_count = EXCLUDED.event_count,
                                content_preview = EXCLUDED.content_preview,
                                finished_at = EXCLUDED.finished_at,
                                updated_at = EXCLUDED.updated_at
                            """,
                            run
                        )
            return True
        except Exception as exc:
            logger.warning("Failed to save runs to PG: {}", exc)
            return False

    async def save_events(self, run_id: str, events: Iterable[dict[str, Any]]) -> bool:
        if not self._enabled:
            return False
        conn = await self._get_conn()
        if not conn:
            return False
        try:
            async with conn:
                async with conn.cursor() as cur:
                    for event in events:
                        await cur.execute(
                            """
                            INSERT INTO observability_events (
                                run_id, ts, event_name, phase, status, component, source,
                                duration_ms, details, metadata
                            ) VALUES (
                                %s, to_timestamp(%s::double precision / 1000.0), %s, %s, %s, %s, %s, %s, %s, %s
                            )
                            """,
                            (
                                run_id, event.get("ts_ms"), event.get("event_name"),
                                event.get("phase"), event.get("status"), event.get("component"),
                                event.get("source"), event.get("duration_ms"),
                                json.dumps(event.get("details")), json.dumps(event.get("metadata") or {})
                            )
                        )
            return True
        except Exception as exc:
            logger.warning("Failed to save events to PG: {}", exc)
            return False

    async def purge_old_data(self, days: int) -> int:
        if not self._enabled:
            return 0
        conn = await self._get_conn()
        if not conn:
            return 0
        try:
            async with conn:
                async with conn.cursor() as cur:
                    # cascade will handle events and feedbacks
                    await cur.execute(
                        "DELETE FROM observability_runs WHERE started_at < NOW() - make_interval(days => %s)",
                        (days,)
                    )
                    return cur.rowcount
        except Exception as exc:
            logger.warning("Failed to purge old PG observability data: {}", exc)
            return 0


def _derive_trace_status(
    current_status: str | None,
    event_name: str,
    phase: str,
    status: str | None,
) -> str:
    normalized_status = str(status or "").strip().lower()
    normalized_phase = str(phase or "").strip().lower()
    if normalized_status in {"failed", "error"} or normalized_phase in _TRACE_FAILURE_PHASES:
        return "failed"
    if event_name == "server.client.delivered":
        return "delivered"
    if event_name in _TRACE_SUCCESS_EVENTS or normalized_status == "completed":
        return "completed"
    if event_name in {"client.message.received", "server.message.accepted"}:
        return "queued"
    if normalized_phase in {"start", "request"}:
        return "processing"
    return current_status or "processing"


def _get_trace_duration_ms(task: dict[str, Any]) -> int | None:
    started_at_ms = _as_int(task.get("started_at_ms"))
    finished_at_ms = _as_int(task.get("delivered_at_ms") or task.get("finished_at_ms") or task.get("updated_at_ms"))
    if started_at_ms <= 0 or finished_at_ms <= 0 or finished_at_ms < started_at_ms:
        return None
    return finished_at_ms - started_at_ms


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(round((len(ordered) - 1) * ratio))
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def _build_trace_summary_payload(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    status_breakdown: dict[str, int] = {}
    component_breakdown: dict[str, int] = {}
    event_breakdown: dict[str, int] = {}
    graph_breakdown: dict[str, int] = {}
    durations: list[int] = []
    total_event_count = 0
    latest_updated_at = 0
    bot_ids: set[str] = set()
    client_ids: set[str] = set()
    active_tasks = 0
    failed_tasks = 0
    completed_tasks = 0
    delivered_tasks = 0
    graph_tasks = 0
    retryable_tasks = 0
    idempotent_tasks = 0
    planner_retry_tasks = 0

    for task in tasks:
        status = str(task.get("status") or "processing").strip() or "processing"
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        if status in {"processing", "queued"}:
            active_tasks += 1
        if status == "failed":
            failed_tasks += 1
        if status == "completed":
            completed_tasks += 1
        if status == "delivered":
            delivered_tasks += 1

        component = str(task.get("last_component") or "").strip()
        if component:
            component_breakdown[component] = component_breakdown.get(component, 0) + 1

        event_name = str(task.get("last_event") or "").strip()
        if event_name:
            event_breakdown[event_name] = event_breakdown.get(event_name, 0) + 1

        graph_id = str(task.get("graph_id") or "").strip()
        if graph_id:
            graph_tasks += 1
            graph_breakdown[graph_id] = graph_breakdown.get(graph_id, 0) + 1

        if _as_bool_or_none(task.get("retryable")) is True:
            retryable_tasks += 1
        if _as_bool_or_none(task.get("idempotent")) is True:
            idempotent_tasks += 1
        if _as_int(task.get("planner_attempt")) > 1:
            planner_retry_tasks += 1

        bot_id = str(task.get("bot_id") or "").strip()
        if bot_id:
            bot_ids.add(bot_id)

        client_id = str(task.get("client_id") or "").strip()
        if client_id:
            client_ids.add(client_id)

        total_event_count += _as_int(task.get("event_count"))
        latest_updated_at = max(latest_updated_at, _as_int(task.get("updated_at_ms")))

        duration_ms = _get_trace_duration_ms(task)
        if duration_ms is not None:
            durations.append(duration_ms)

    total_tasks = len(tasks)
    top_components = [
        {"name": name, "count": count}
        for name, count in sorted(component_breakdown.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    top_events = [
        {"name": name, "count": count}
        for name, count in sorted(event_breakdown.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    top_graphs = [
        {"name": name, "count": count}
        for name, count in sorted(graph_breakdown.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    return {
        "total_tasks": total_tasks,
        "active_tasks": active_tasks,
        "failed_tasks": failed_tasks,
        "completed_tasks": completed_tasks,
        "delivered_tasks": delivered_tasks,
        "graph_tasks": graph_tasks,
        "retryable_tasks": retryable_tasks,
        "idempotent_tasks": idempotent_tasks,
        "planner_retry_tasks": planner_retry_tasks,
        "status_breakdown": status_breakdown,
        "avg_event_count": round(total_event_count / total_tasks, 2) if total_tasks else 0,
        "avg_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
        "p95_duration_ms": _percentile(durations, 0.95),
        "unique_bot_count": len(bot_ids),
        "unique_client_count": len(client_ids),
        "latest_updated_at": latest_updated_at,
        "top_components": top_components,
        "top_events": top_events,
        "top_graphs": top_graphs,
    }


class ObservabilityStore:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None
        self._uri: str | None = None
        self._config: ObservabilityConfig | None = None
        self._lock = Lock()
        self._max_audit_entries = 2000
        self._max_trace_entries = 4000
        self._max_trace_tasks = 2000
        self._memory_trace_tasks: dict[str, dict[str, Any]] = {}
        self._memory_trace_events: dict[str, list[dict[str, Any]]] = {}
        self._memory_trace_recent: list[str] = []
        self._recent_trace_fingerprints: dict[str, int] = {}

        # Async Queue Support
        self._queue: asyncio.Queue | None = None
        self._worker_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

        # Persistence & Janitor
        self._persistence: ObservabilityPersistence | None = None
        self._janitor_task: asyncio.Task | None = None

    def _resolve_is_admin(self, requested: bool, account_id: str | None) -> bool:
        """Resolve admin access centrally.

        Lower layers must not trust `is_admin=True` directly. This function enforces
        a config-based allowlist to guard global indices.
        """
        if not requested:
            return False
        runtime_account = context.get_runtime_context().get("account_id")
        normalized = _normalize_text_filter(account_id or runtime_account)
        if not normalized:
            return False
        allowlist: set[str] = set()
        if self._config and isinstance(getattr(self._config, "admin_account_ids", None), list):
            allowlist.update(
                _normalize_text_filter(str(item))
                for item in (self._config.admin_account_ids or [])
                if _normalize_text_filter(str(item))
            )
        for env_name in ("ITHQBOT_TRACE_ADMIN_ACCOUNTS", "ITHQBOT_OBSERVABILITY_ADMIN_ACCOUNTS"):
            raw = os.getenv(env_name, "")
            for item in raw.split(","):
                normalized_item = _normalize_text_filter(item)
                if normalized_item:
                    allowlist.add(normalized_item)
        if not allowlist:
            return False
        return normalized in allowlist

    def _resolve_trace_tenant_id(self, tenant_id: str | None) -> str | None:
        runtime_tenant = context.get_runtime_context().get("tenant_id")
        return _normalize_text_filter(tenant_id or runtime_tenant)

    def configure(self, uri: str | None, config: ObservabilityConfig | None = None) -> None:
        with self._lock:
            self._config = config
            if config and config.pg_uri:
                self._persistence = ObservabilityPersistence(config.pg_uri)
            else:
                self._persistence = None

            if not uri:
                self._client = None
                self._uri = None
                self._stop_worker_sync()
                return
            if not uri.startswith(("redis://", "rediss://")):
                self._client = None
                self._uri = None
                self._stop_worker_sync()
                return
            if uri == self._uri and self._client is not None:
                self._ensure_worker_running()
                return
            client = redis.Redis.from_url(
                uri,
                decode_responses=True,
                health_check_interval=30,
                socket_connect_timeout=_redis_connect_timeout_seconds(),
                socket_timeout=_redis_socket_timeout_seconds(),
            )
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    client.ping()
                    self._client = client
                    self._uri = uri
                    self._ensure_worker_running()
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        time.sleep(0.1 * (2 ** (attempt - 1)))
            logger.warning("Observability Redis unavailable after retries: {}", last_error)
            self._client = None
            self._uri = None
            self._stop_worker_sync()

    def _client_or_none(self) -> redis.Redis | None:
        return self._client

    def _try_enqueue(self, kind: str, args: dict[str, Any]) -> bool:
        """Attempt to enqueue an event for async processing. Returns True if successful."""
        if not self._queue or not self._config or not self._config.async_mode:
            return False

        try:
            self._queue.put_nowait({"_kind": kind, "_args": args})
            return True
        except asyncio.QueueFull:
            logger.warning("Observability queue full, falling back to sync recording")
            return False
        except Exception as exc:
            logger.warning("Failed to enqueue observability event: {}", exc)
            return False

    def _ensure_worker_running(self) -> None:
        """Ensure the async background worker and janitor are running if enabled."""
        config = self._config
        if not config or not config.enabled:
            self._stop_worker_sync()
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        # Start Async Reporting Worker
        if config.async_mode:
            if not (self._worker_task and not self._worker_task.done()):
                self._queue = asyncio.Queue(maxsize=config.queue_size)
                self._stop_event = asyncio.Event()
                self._worker_task = loop.create_task(self._async_worker_loop())
                logger.info("Observability async worker started (queue_size={})", config.queue_size)

    def _should_record_trace_event(self, args: dict[str, Any]) -> bool:
        config = self._config
        if not config or not bool(getattr(config, "enabled", True)):
            return True

        event_name = str(args.get("event_name") or "").strip()
        if not event_name:
            return True

        exempt = {
            str(item).strip()
            for item in (getattr(config, "trace_sample_exempt_events", []) or [])
            if str(item).strip()
        }
        if event_name in exempt:
            return True

        noisy_events = {
            str(item).strip()
            for item in (getattr(config, "noisy_trace_events", []) or [])
            if str(item).strip()
        }
        now_ms = _now_ms()
        suppression_window_ms = max(0, int(getattr(config, "trace_noise_suppression_window_ms", 0) or 0))
        if suppression_window_ms > 0 and event_name in noisy_events:
            fingerprint = "|".join(
                [
                    event_name,
                    str(args.get("phase") or "").strip(),
                    str(args.get("component") or "").strip(),
                    str(args.get("request_msg_id") or "").strip(),
                    str(args.get("trace_id") or "").strip(),
                    str(args.get("chat_id") or "").strip(),
                ]
            )
            last_seen_ms = self._recent_trace_fingerprints.get(fingerprint)
            if last_seen_ms is not None and now_ms - last_seen_ms < suppression_window_ms:
                return False
            self._recent_trace_fingerprints[fingerprint] = now_ms
            if len(self._recent_trace_fingerprints) > 20_000:
                cutoff = now_ms - suppression_window_ms
                self._recent_trace_fingerprints = {
                    key: ts for key, ts in self._recent_trace_fingerprints.items() if ts >= cutoff
                }

        sample_rate_raw = getattr(config, "trace_sample_rate", 1.0)
        sample_rate = 1.0 if sample_rate_raw is None else float(sample_rate_raw)
        if sample_rate >= 1.0 or event_name not in noisy_events:
            return True
        if sample_rate <= 0:
            return False
        return random.random() < sample_rate

        # Start Janitor Task (Retention & Persistence)
        if self._persistence:
            if not (self._janitor_task and not self._janitor_task.done()):
                self._janitor_task = loop.create_task(self._janitor_loop())
                logger.info("Observability janitor started")

    def _stop_worker_sync(self) -> None:
        """Request the worker and janitor to stop."""
        if self._stop_event:
            self._stop_event.set()
        self._worker_task = None
        self._janitor_task = None

    async def _janitor_loop(self) -> None:
        """Background loop to archive Redis data to PG and prune old records."""
        config = self._config
        if not config or not self._persistence:
            return

        # Wait a bit after startup
        await asyncio.sleep(60)

        while self._stop_event and not self._stop_event.is_set():
            try:
                # 1. Archive Redis to PG (those older than retention or completed)
                await self._archive_redis_to_pg()

                # 2. Purge old PG records
                if config.pg_retention_d > 0:
                    purged = await self._persistence.purge_old_data(config.pg_retention_d)
                    if purged > 0:
                        logger.info("Janitor: Purged {} old runs from PostgreSQL", purged)

            except Exception as exc:
                logger.warning("Janitor loop error: {}", exc)

            # Sleep for an hour
            await asyncio.sleep(3600)

    async def _archive_redis_to_pg(self) -> None:
        """Move traces from Redis to PG based on age or status."""
        client = self._client_or_none()
        if not client or not self._persistence or not self._config:
            return

        retention_ms = self._config.redis_retention_h * 3600 * 1000
        now_ms = _now_ms()

        # We use the global 'recent' index to find candidates
        try:
            loop = asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, client.lrange, "ithqbot:trace:index:recent", 0, -1)
        except Exception:
            return

        for entry in entries:
            raw_entry = str(entry or "").strip()
            if not raw_entry:
                continue

            # entry is "tid:rid"
            if ":" not in raw_entry:
                continue

            tid, rid = raw_entry.split(":", 1)
            summary_key = f"ithqbot:trace:tenant:{tid}:task:{rid}"

            try:
                task = await loop.run_in_executor(None, client.hgetall, summary_key)
            except Exception:
                continue

            if not task:
                continue

            updated_at = _as_int(task.get("updated_at_ms"))
            status = task.get("status")

            # Archive if finished OR older than retention
            should_archive = status in {"completed", "failed", "delivered"} or (now_ms - updated_at) > retention_ms

            if should_archive:
                # Fetch events
                events_key = f"{summary_key}:events"
                try:
                    events_raw = await loop.run_in_executor(None, client.lrange, events_key, 0, -1)
                    events = [json.loads(e) for e in events_raw]
                except Exception:
                    events = []

                # Save to PG
                success = await self._persistence.save_runs([task])
                if success and events:
                    await self._persistence.save_events(rid, events)

                if success:
                    # Cleanup Redis (offload to thread)
                    await loop.run_in_executor(None, self._sync_cleanup_redis_archive, summary_key, events_key)

    def _sync_cleanup_redis_archive(self, summary_key: str, events_key: str) -> None:
        client = self._client_or_none()
        if client:
            try:
                client.delete(summary_key, events_key)
            except Exception:
                pass

    async def _async_worker_loop(self) -> None:
        """Background loop to flush queued events to Redis."""
        config = self._config
        if not config:
            return

        batch_size = max(1, config.batch_size)
        flush_interval = max(0.1, config.flush_interval_s)

        while self._stop_event and not self._stop_event.is_set():
            batch: list[dict[str, Any]] = []
            try:
                # Try to get the first item
                item = await asyncio.wait_for(self._queue.get(), timeout=flush_interval)
                batch.append(item)

                # Try to fill the batch until it's full or empty
                while len(batch) < batch_size:
                    try:
                        item = self._queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

            if batch:
                await self._flush_batch_to_redis(batch)
                for _ in range(len(batch)):
                    self._queue.task_done()

        # Flush remaining items on stop
        if self._queue and not self._queue.empty():
            remaining = []
            while not self._queue.empty():
                remaining.append(self._queue.get_nowait())
            if remaining:
                await self._flush_batch_to_redis(remaining)

    async def _flush_batch_to_redis(self, batch: list[dict[str, Any]]) -> None:
        client = self._client_or_none()
        if client is None:
            return

        try:
            # We'll use a local pipeline for efficiency
            # Since redis-py's pipeline is not inherently async in the synchronous client,
            # we run it in a thread pool if needed, but for now we assume the client is compatible
            # or we just use normal client calls.
            # Actually, to keep it simple and safe with the current redis-py (sync),
            # we'll use run_in_executor if it's a sync client.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._sync_flush_batch, batch)
        except Exception as exc:
            logger.warning("Failed to flush observability batch: {}", exc)

    def _sync_flush_batch(self, batch: list[dict[str, Any]]) -> None:
        client = self._client_or_none()
        if client is None:
            return

        # Pre-fetch existing summaries for trace events to avoid blocking calls in the loop
        trace_summaries: dict[str, dict[str, Any]] = {}
        trace_items = [item for item in batch if item.get("_kind") == "trace"]
        
        if trace_items:
            # Use a pipeline to fetch all summaries in one go
            fetch_pipe = client.pipeline()
            # We need to map summary_key to req_id to store the results
            key_to_req_id: dict[str, str] = {}
            for item in trace_items:
                ctx = self._resolve_trace_context(**item.get("_args", {}))
                summary_key = ctx.get("summary_key")
                req_id = ctx.get("req_id")
                if summary_key and req_id:
                    if summary_key not in key_to_req_id:
                        fetch_pipe.hgetall(summary_key)
                        key_to_req_id[summary_key] = req_id
            
            if key_to_req_id:
                try:
                    results = fetch_pipe.execute()
                    for summary_key, summary_data in zip(key_to_req_id.keys(), results):
                        req_id = key_to_req_id[summary_key]
                        # Convert bytes keys to strings if necessary (redis-py usually does this with decode_responses=True)
                        trace_summaries[req_id] = summary_data if isinstance(summary_data, dict) else {}
                except Exception as exc:
                    logger.warning("Failed to pre-fetch trace summaries: {}", exc)

        pipe = client.pipeline()
        for item in batch:
            kind = item.get("_kind")
            args = item.get("_args", {})
            if kind == "trace":
                ctx = self._resolve_trace_context(**args)
                req_id = ctx.get("req_id")
                existing = trace_summaries.get(req_id) if req_id else None
                self._record_trace_event_sync(pipe, existing=existing, **args)
            elif kind == "audit":
                self._record_audit_sync(pipe, **args)
            elif kind == "llm_usage":
                self._record_llm_usage_sync(pipe, **args)

        try:
            pipe.execute()
        except Exception as exc:
            logger.warning("Pipe execute failed in batch flush: {}", exc)


    def _process_single_event_sync(self, pipe: Any, item: dict[str, Any]) -> None:
        """Handles the actual Redis logic for a single event (audit or trace)."""
        kind = item.get("_kind")
        if kind == "audit":
            self._record_audit_sync(pipe, **item.get("_args", {}))
        elif kind == "trace":
            self._record_trace_event_sync(pipe, **item.get("_args", {}))
        elif kind == "llm_usage":
            self._record_llm_usage_sync(pipe, **item.get("_args", {}))

    def _record_trace_event_in_memory(self, *, req_id: str, summary: dict[str, Any], event: dict[str, Any]) -> None:
        with self._lock:
            self._memory_trace_tasks[req_id] = dict(summary)
            history = self._memory_trace_events.setdefault(req_id, [])
            history.append(dict(event))
            if len(history) > self._max_trace_entries:
                del history[:-self._max_trace_entries]
            self._memory_trace_recent = [item for item in self._memory_trace_recent if item != req_id]
            self._memory_trace_recent.insert(0, req_id)
            if len(self._memory_trace_recent) > self._max_trace_tasks:
                overflow = self._memory_trace_recent[self._max_trace_tasks :]
                self._memory_trace_recent = self._memory_trace_recent[: self._max_trace_tasks]
                for stale_req_id in overflow:
                    self._memory_trace_tasks.pop(stale_req_id, None)
                    self._memory_trace_events.pop(stale_req_id, None)

    def _get_trace_tasks_from_memory(
        self,
        *,
        account_id: str | None = None,
        tenant_id: str | None = None,
        is_admin: bool = False,
        chat_id: str | None = None,
        client_id: str | None = None,
        bot_id: str | None = None,
        channel: str | None = None,
        status: str | None = None,
        start_at: int | None = None,
        end_at: int | None = None,
        keyword: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized_limit = _normalize_limit(limit)
        normalized_account = _normalize_text_filter(account_id)
        normalized_tenant = _normalize_text_filter(tenant_id)
        normalized_bot = _normalize_text_filter(bot_id)
        normalized_chat = _normalize_text_filter(chat_id)
        normalized_client = _normalize_text_filter(client_id)
        normalized_channel = _normalize_text_filter(channel)
        normalized_status = _normalize_text_filter(status)
        normalized_keyword = _normalize_text_filter(keyword)
        tasks: list[dict[str, Any]] = []
        with self._lock:
            request_ids = list(self._memory_trace_recent)
            summaries = dict(self._memory_trace_tasks)
        for req in request_ids:
            task = dict(summaries.get(req) or {})
            if not task:
                continue
            updated_at_ms = _as_int(task.get("updated_at_ms"))

            if not is_admin and normalized_tenant and not _match_filter(task.get("tenant_id"), normalized_tenant):
                continue
            if normalized_account and not _match_filter(task.get("account_id"), normalized_account):
                continue
            if not _match_filter(task.get("bot_id"), normalized_bot):
                continue
            if not _match_filter(task.get("chat_id"), normalized_chat):
                continue
            if not _match_filter(task.get("client_id"), normalized_client):
                continue
            if not _match_filter(task.get("channel"), normalized_channel):
                continue
            if not _match_filter(task.get("status"), normalized_status):
                continue
            if isinstance(start_at, int) and updated_at_ms < start_at:
                continue
            if isinstance(end_at, int) and updated_at_ms > end_at:
                continue
            if normalized_keyword and normalized_keyword not in str(task.get("search_text") or "").lower():
                continue
            tasks.append(task)
            if len(tasks) >= normalized_limit:
                break
        return tasks

    def is_healthy(self) -> bool:
        """Check if the store is healthy (Redis ping or memory fallback)."""
        client = self._client_or_none()
        if client is None:
            # If no URI is configured, we are in memory-only mode which is "healthy"
            return self._uri is None
        try:
            return bool(client.ping())
        except Exception:
            return False

    def record_audit(
        self,
        *,
        call_type: str,
        name: str,
        phase: str,
        input_data: Any = None,
        output_data: Any = None,
        interaction_data: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        args = {
            "call_type": call_type,
            "name": name,
            "phase": phase,
            "input_data": input_data,
            "output_data": output_data,
            "interaction_data": interaction_data,
            "extra": extra,
        }
        if self._try_enqueue("audit", args):
            return

        client = self._client_or_none()
        if client is None:
            return
        
        # NOTE: Sync path should be avoided in production
        pipe = client.pipeline()
        self._record_audit_sync(pipe, **args)
        try:
            pipe.execute()
        except Exception as exc:
            logger.warning("Failed to execute sync audit pipeline: {}", exc)

    def _record_audit_sync(
        self,
        pipe: Any,
        *,
        call_type: str,
        name: str,
        phase: str,
        input_data: Any = None,
        output_data: Any = None,
        interaction_data: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        runtime = context.get_runtime_context()
        tenant_id = _safe_key_part(runtime.get("tenant_id"), "default")
        account = runtime.get("account_id") or "anonymous"
        bot = _safe_key_part(runtime.get("bot_id"), "default")
        event = {
            "ts": time.time(),
            "account_id": account,
            "tenant_id": runtime.get("tenant_id"),
            "bot_id": runtime.get("bot_id"),
            "trace_id": runtime.get("trace_id"),
            "channel": runtime.get("channel"),
            "chat_id": runtime.get("chat_id"),
            "call_type": call_type,
            "name": name,
            "phase": phase,
            "input": _compact(input_data),
            "output": _compact(output_data),
            "interaction": _compact(interaction_data),
            "passthrough": _compact(runtime.get("metadata")),
            "extra": _compact(extra or {}),
        }
        keys = [
            f"ithqbot:trace:tenant:{tenant_id}:audit:{account}",
            f"ithqbot:trace:tenant:{tenant_id}:audit:{account}:bot:{bot}",
            f"ithqbot:trace:audit:global:{account}", # Global index for admin
        ]
        payload = json.dumps(event, ensure_ascii=False, default=str)
        for key in keys:
            pipe.lpush(key, payload)
            pipe.ltrim(key, 0, self._max_audit_entries - 1)

        self._record_trace_event_sync(
            pipe,
            event_name=f"{call_type}.{name}",
            phase=phase,
            component=name,
            source=call_type,
            details={
                "input": _compact(input_data, max_chars=1200),
                "output": _compact(output_data, max_chars=1200),
                "interaction": _compact(interaction_data, max_chars=1200),
                "extra": _compact(extra or {}, max_chars=1200),
            },
        )

    def record_llm_usage(
        self,
        *,
        source: str,
        model: str | None,
        usage: dict[str, Any] | None,
        finish_reason: str,
        component: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        args = {
            "source": source,
            "model": model,
            "usage": usage,
            "finish_reason": finish_reason,
            "component": component,
            "duration_ms": duration_ms,
        }
        if self._try_enqueue("llm_usage", args):
            return

        client = self._client_or_none()
        if client is None:
            return
        
        # NOTE: Sync path should be avoided in production
        pipe = client.pipeline()
        self._record_llm_usage_sync(pipe, **args)
        try:
            pipe.execute()
        except Exception as exc:
            logger.warning("Failed to execute sync llm usage pipeline: {}", exc)

    def _record_llm_usage_sync(
        self,
        pipe: Any,
        *,
        source: str,
        model: str | None,
        usage: dict[str, Any] | None,
        finish_reason: str,
        component: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        runtime = context.get_runtime_context()
        tenant_id = _safe_key_part(runtime.get("tenant_id"), "default")
        account = runtime.get("account_id") or "anonymous"
        bot = _safe_key_part(runtime.get("bot_id"), "default")
        src = source if source in _SOURCES else "bot"
        usage_obj = usage or {}
        req_tokens = int(usage_obj.get("prompt_tokens") or usage_obj.get("input_tokens") or 0)
        resp_tokens = int(usage_obj.get("completion_tokens") or usage_obj.get("output_tokens") or 0)
        total_tokens = int(usage_obj.get("total_tokens") or (req_tokens + resp_tokens))
        scope_keys = [
            (
                f"ithqbot:trace:tenant:{tenant_id}:llm:stats:{account}:{src}",
                f"ithqbot:trace:tenant:{tenant_id}:llm:stats:{account}:{src}:model:{model or 'default'}",
            ),
            (
                f"ithqbot:trace:tenant:{tenant_id}:llm:stats:{account}:bot:{bot}:{src}",
                f"ithqbot:trace:tenant:{tenant_id}:llm:stats:{account}:bot:{bot}:{src}:model:{model or 'default'}",
            ),
            (
                f"ithqbot:trace:llm:stats:{account}:{src}",
                f"ithqbot:trace:llm:stats:{account}:{src}:model:{model or 'default'}",
            ),
            (
                f"ithqbot:trace:llm:stats:{account}:bot:{bot}:{src}",
                f"ithqbot:trace:llm:stats:{account}:bot:{bot}:{src}:model:{model or 'default'}",
            ),
        ]
        now = str(int(time.time()))
        for key, model_key in scope_keys:
            pipe.hincrby(key, "request_count", 1)
            pipe.hincrby(key, "request_tokens", req_tokens)
            pipe.hincrby(key, "response_tokens", resp_tokens)
            pipe.hincrby(key, "total_tokens", total_tokens)
            pipe.hset(
                key,
                mapping={"last_model": model or "", "last_finish_reason": finish_reason, "updated_at": now},
            )
            pipe.hincrby(model_key, "request_count", 1)
            pipe.hincrby(model_key, "request_tokens", req_tokens)
            pipe.hincrby(model_key, "response_tokens", resp_tokens)
            pipe.hincrby(model_key, "total_tokens", total_tokens)
            pipe.hset(model_key, mapping={"component": component or "", "updated_at": now})

        self._record_trace_event_sync(
            pipe,
            event_name="llm.call",
            phase="response",
            status="completed",
            component=component or model or src,
            source=src,
            duration_ms=duration_ms,
            details={
                "model": model,
                "usage": usage_obj,
                "finish_reason": finish_reason,
            },
        )

    def record_trace_event(
        self,
        *,
        event_name: str,
        phase: str = "point",
        status: str | None = None,
        component: str | None = None,
        source: str | None = None,
        duration_ms: int | None = None,
        details: Any = None,
        account_id: str | None = None,
        tenant_id: str | None = None,
        bot_id: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        client_id: str | None = None,
        request_msg_id: str | None = None,
        trace_id: str | None = None,
        parent_run_id: str | None = None,
        content_preview: str | None = None,
        event_type: str | None = None,
    ) -> None:
        args = {
            "event_name": event_name,
            "phase": phase,
            "status": status,
            "component": component,
            "source": source,
            "duration_ms": duration_ms,
            "details": details,
            "account_id": account_id,
            "tenant_id": tenant_id,
            "bot_id": bot_id,
            "channel": channel,
            "chat_id": chat_id,
            "client_id": client_id,
            "request_msg_id": request_msg_id,
            "trace_id": trace_id,
            "parent_run_id": parent_run_id,
            "content_preview": content_preview,
            "event_type": event_type,
        }
        if not self._should_record_trace_event(args):
            return

        if self._try_enqueue("trace", args):
            return

        client = self._client_or_none()
        if client is None:
            # Memory fallback handled directly in sync method
            self._record_trace_event_sync(None, **args)
            return

        # NOTE: Sync path should be avoided in production
        pipe = client.pipeline()
        self._record_trace_event_sync(pipe, **args)
        try:
            pipe.execute()
        except Exception as exc:
            logger.warning("Failed to execute sync trace event pipeline: {}", exc)

    def _resolve_trace_context(self, **args) -> dict[str, Any]:
        runtime = context.get_runtime_context()
        req_id = (
            args.get("request_msg_id")
            or runtime.get("request_msg_id")
            or runtime.get("message_id")
        )
        req_id = str(req_id or "").strip()
        if not req_id:
            return {}

        runtime_trace_id = runtime.get("trace_id")
        resolved_trace_id = str(args.get("trace_id") or runtime_trace_id or req_id).strip()
        resolved_account = str(args.get("account_id") or runtime.get("account_id") or "anonymous").strip() or "anonymous"
        resolved_bot_id = str(args.get("bot_id") or runtime.get("bot_id") or "").strip()
        resolved_channel = str(args.get("channel") or runtime.get("channel") or "").strip()
        resolved_chat_id = str(args.get("chat_id") or runtime.get("chat_id") or "").strip()
        resolved_client_id = str(args.get("client_id") or runtime.get("client_id") or "").strip()
        resolved_tenant_id = str(args.get("tenant_id") or runtime.get("tenant_id") or "").strip()
        resolved_event_type = str(args.get("event_type") or runtime.get("event_type") or "").strip()
        resolved_parent_id = str(args.get("parent_run_id") or "").strip() or None

        content_preview = args.get("content_preview")
        preview = (
            str(content_preview).strip()
            if isinstance(content_preview, str) and content_preview.strip()
            else _short_text(runtime.get("metadata", {}).get("content") if isinstance(runtime.get("metadata"), dict) else None)
        )

        tenant_prefix = _safe_key_part(resolved_tenant_id, "default")
        summary_key = f"ithqbot:trace:tenant:{tenant_prefix}:task:{req_id}"
        events_key = f"{summary_key}:events"

        return {
            "req_id": req_id,
            "trace_id": resolved_trace_id,
            "account_id": resolved_account,
            "bot_id": resolved_bot_id,
            "channel": resolved_channel,
            "chat_id": resolved_chat_id,
            "client_id": resolved_client_id,
            "tenant_id": resolved_tenant_id,
            "event_type": resolved_event_type,
            "parent_run_id": resolved_parent_id,
            "preview": preview,
            "tenant_prefix": tenant_prefix,
            "summary_key": summary_key,
            "events_key": events_key,
        }

    def _record_trace_event_sync(
        self,
        pipe: Any | None,
        *,
        event_name: str,
        phase: str = "point",
        status: str | None = None,
        component: str | None = None,
        source: str | None = None,
        duration_ms: int | None = None,
        details: Any = None,
        account_id: str | None = None,
        tenant_id: str | None = None,
        bot_id: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        client_id: str | None = None,
        request_msg_id: str | None = None,
        trace_id: str | None = None,
        parent_run_id: str | None = None,
        content_preview: str | None = None,
        event_type: str | None = None,
        existing: dict[str, Any] | None = None,
    ) -> None:
        ctx = self._resolve_trace_context(
            request_msg_id=request_msg_id,
            trace_id=trace_id,
            account_id=account_id,
            tenant_id=tenant_id,
            bot_id=bot_id,
            channel=channel,
            chat_id=chat_id,
            client_id=client_id,
            content_preview=content_preview,
            event_type=event_type,
            parent_run_id=parent_run_id,
        )
        req_id = ctx.get("req_id")
        if not req_id:
            return

        resolved_trace_id = ctx["trace_id"]
        resolved_account = ctx["account_id"]
        resolved_bot_id = ctx["bot_id"]
        resolved_channel = ctx["channel"]
        resolved_chat_id = ctx["chat_id"]
        resolved_client_id = ctx["client_id"]
        resolved_tenant_id = ctx["tenant_id"]
        resolved_event_type = ctx["event_type"]
        resolved_parent_id = ctx["parent_run_id"]
        preview = ctx["preview"]
        tenant_prefix = ctx["tenant_prefix"]
        summary_key = ctx["summary_key"]
        events_key = ctx["events_key"]

        client = self._client_or_none()
        ts_ms = _now_ms()

        if existing is None:
            if client is None:
                with self._lock:
                    existing = dict(self._memory_trace_tasks.get(req_id) or {})
            else:
                try:
                    # NOTE: This is a blocking call. Batch flushes should pre-fetch.
                    existing = client.hgetall(summary_key)
                except Exception:
                    existing = {}
        
        # Ensure it's a dict
        existing = existing or {}


        next_status = _derive_trace_status(existing.get("status"), event_name, phase, status)
        next_event_count = _as_int(existing.get("event_count")) + 1
        next_preview = preview or existing.get("content_preview") or ""

        new_total_tokens = _as_int(existing.get("total_tokens"))
        next_last_model = existing.get("last_model") or ""
        if event_name == "llm.call" and isinstance(details, dict):
            usage = details.get("usage") or {}
            add_tokens = _as_int(usage.get("total_tokens"))
            if not add_tokens:
                add_tokens = _as_int(usage.get("prompt_tokens") or usage.get("input_tokens")) + _as_int(usage.get("completion_tokens") or usage.get("output_tokens"))
            new_total_tokens += add_tokens
            if details.get("model"):
                next_last_model = str(details.get("model"))

        event = {
            "ts_ms": ts_ms,
            "request_msg_id": req_id,
            "trace_id": resolved_trace_id,
            "parent_run_id": resolved_parent_id,
            "account_id": resolved_account,
            "tenant_id": resolved_tenant_id or None,
            "bot_id": resolved_bot_id or None,
            "channel": resolved_channel or None,
            "chat_id": resolved_chat_id or None,
            "client_id": resolved_client_id or None,
            "source": source or None,
            "event_type": resolved_event_type or None,
            "event_name": event_name,
            "phase": phase,
            "status": status or next_status,
            "component": component,
            "duration_ms": duration_ms,
            "content_preview": next_preview or None,
            "details": _compact(details, max_chars=2000),
        }
        extracted_dimensions = _extract_trace_dimensions(event["details"])
        event.update(extracted_dimensions)
        summary = {
            "request_msg_id": req_id,
            "trace_id": resolved_trace_id,
            "account_id": resolved_account,
            "tenant_id": resolved_tenant_id,
            "bot_id": resolved_bot_id,
            "channel": resolved_channel,
            "chat_id": resolved_chat_id,
            "client_id": resolved_client_id,
            "status": next_status,
            "last_event": event_name,
            "last_phase": phase,
            "last_component": component or "",
            "last_source": source or "",
            "last_model": next_last_model,
            "total_tokens": new_total_tokens,
            "event_type": resolved_event_type,
            "content_preview": next_preview,
            "updated_at_ms": str(ts_ms),
            "search_text": "",
        }
        if resolved_parent_id:
            summary["parent_run_id"] = resolved_parent_id
        if extracted_dimensions.get("graph_id"):
            summary["graph_id"] = extracted_dimensions["graph_id"]
        if extracted_dimensions.get("run_id"):
            summary["run_id"] = extracted_dimensions["run_id"]
        if extracted_dimensions.get("node_id"):
            summary["node_id"] = extracted_dimensions["node_id"]
        planner_attempt = extracted_dimensions.get("planner_attempt")
        if isinstance(planner_attempt, int) and planner_attempt > 0:
            summary["planner_attempt"] = planner_attempt
        for bool_key in ("retryable", "idempotent"):
            if bool_key in extracted_dimensions:
                summary[bool_key] = extracted_dimensions[bool_key]
        if not existing.get("started_at_ms"):
            summary["started_at_ms"] = str(ts_ms)
        if next_status in {"completed", "failed", "delivered"}:
            summary["finished_at_ms"] = str(ts_ms)
        if next_status == "delivered":
            summary["delivered_at_ms"] = str(ts_ms)

        normalized_existing = self._normalize_trace_task(existing)
        existing_attempt = _as_int(normalized_existing.get("planner_attempt"))
        next_attempt = _as_int(summary.get("planner_attempt"))
        if existing_attempt > next_attempt:
            summary["planner_attempt"] = existing_attempt
        for bool_key in ("retryable", "idempotent"):
            if bool_key not in summary and normalized_existing.get(bool_key) is not None:
                summary[bool_key] = normalized_existing[bool_key]
        for key in ("graph_id", "run_id", "node_id", "parent_run_id"):
            if not summary.get(key) and normalized_existing.get(key):
                summary[key] = normalized_existing[key]
        normalized_existing.update(summary)
        normalized_existing["event_count"] = next_event_count
        normalized_existing["search_text"] = _build_trace_search_text(normalized_existing)

        if client is None:
            self._record_trace_event_in_memory(req_id=req_id, summary=normalized_existing, event=event)
            return

        # If pipe was not provided (sync caller that didn't create one yet), create it
        actual_pipe = pipe if pipe is not None else client.pipeline()

        try:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            lookup_ttl_ms = max(1, int((self._config.redis_retention_h if self._config else 24) * 3600 * 1000))
            actual_pipe.lpush(events_key, payload)
            actual_pipe.ltrim(events_key, 0, self._max_trace_entries - 1)
            actual_pipe.hincrby(summary_key, "event_count", 1)
            actual_pipe.hset(summary_key, mapping={k: str(v) for k, v in normalized_existing.items() if v is not None})
            actual_pipe.set(f"ithqbot:trace:lookup:{req_id}", tenant_prefix, px=lookup_ttl_ms)

            # Indexing (using tenant-aware keys)
            tenant_recent_key = f"ithqbot:trace:tenant:{tenant_prefix}:index:recent"
            tenant_account_key = f"ithqbot:trace:tenant:{tenant_prefix}:index:account:{resolved_account}"

            # Global indices for admin (stores tenant_id:req_id)
            global_recent_key = "ithqbot:trace:index:recent"
            global_account_key = f"ithqbot:trace:index:account:{resolved_account}"

            global_val = f"{tenant_prefix}:{req_id}"

            for rk in [tenant_recent_key]:
                actual_pipe.lpush(rk, req_id)
                actual_pipe.ltrim(rk, 0, self._max_trace_tasks - 1)

            for rk in [global_recent_key]:
                actual_pipe.lpush(rk, global_val)
                actual_pipe.ltrim(rk, 0, self._max_trace_tasks - 1)

            for ak in [tenant_account_key]:
                actual_pipe.lpush(ak, req_id)
                actual_pipe.ltrim(ak, 0, self._max_trace_tasks - 1)

            for ak in [global_account_key]:
                actual_pipe.lpush(ak, global_val)
                actual_pipe.ltrim(ak, 0, self._max_trace_tasks - 1)

            if resolved_bot_id:
                bot_part = _safe_key_part(resolved_bot_id, "default")
                actual_pipe.lpush(f"{tenant_account_key}:bot:{bot_part}", req_id)
                actual_pipe.ltrim(f"{tenant_account_key}:bot:{bot_part}", 0, self._max_trace_tasks - 1)
                actual_pipe.lpush(f"{global_account_key}:bot:{bot_part}", global_val)
                actual_pipe.ltrim(f"{global_account_key}:bot:{bot_part}", 0, self._max_trace_tasks - 1)

            # If we created our own pipeline, execute it
            if pipe is None:
                actual_pipe.execute()
        except Exception as exc:
            logger.warning("Failed to write trace event in sync helper: {}", exc)

    def _normalize_trace_task(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not raw:
            return {}
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            if key in {
                "event_count",
                "started_at_ms",
                "updated_at_ms",
                "finished_at_ms",
                "delivered_at_ms",
                "total_tokens",
                "planner_attempt",
            }:
                normalized[key] = _as_int(value)
            elif key in {"retryable", "idempotent"}:
                normalized[key] = _as_bool_or_none(value)
            else:
                normalized[key] = value
        return normalized

    def get_trace_tasks(
        self,
        *,
        account_id: str | None = None,
        tenant_id: str | None = None,
        is_admin: bool = False,
        requester_account_id: str | None = None,
        chat_id: str | None = None,
        client_id: str | None = None,
        bot_id: str | None = None,
        channel: str | None = None,
        status: str | None = None,
        start_at: int | None = None,
        end_at: int | None = None,
        keyword: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        resolved_tenant_id = self._resolve_trace_tenant_id(tenant_id)
        is_admin = self._resolve_is_admin(is_admin, requester_account_id)
        client = self._client_or_none()
        if client is None:
            return self._get_trace_tasks_from_memory(
                account_id=account_id,
                tenant_id=resolved_tenant_id,
                is_admin=is_admin,
                chat_id=chat_id,
                client_id=client_id,
                bot_id=bot_id,
                channel=channel,
                status=status,
                start_at=start_at,
                end_at=end_at,
                keyword=keyword,
                limit=limit,
            )
        normalized_limit = _normalize_limit(limit)
        normalized_account = _normalize_text_filter(account_id)
        normalized_bot = _normalize_text_filter(bot_id)
        normalized_chat = _normalize_text_filter(chat_id)
        normalized_client = _normalize_text_filter(client_id)
        normalized_channel = _normalize_text_filter(channel)
        normalized_status = _normalize_text_filter(status)
        normalized_keyword = _normalize_text_filter(keyword)

        # Key Resolution
        if is_admin:
            # Admin uses global indices
            if normalized_account:
                index_key = f"ithqbot:trace:index:account:{normalized_account}"
            else:
                index_key = "ithqbot:trace:index:recent"
        else:
            # Normal users use tenant-specific indices
            tenant_prefix = _safe_key_part(resolved_tenant_id, "default")
            if normalized_account:
                index_key = f"ithqbot:trace:tenant:{tenant_prefix}:index:account:{normalized_account}"
            else:
                index_key = f"ithqbot:trace:tenant:{tenant_prefix}:index:recent"

        try:
            request_ids = client.lrange(index_key, 0, self._max_trace_tasks - 1)
        except Exception:
            request_ids = []

        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()

        tenant_prefix = _safe_key_part(resolved_tenant_id, "default")

        for entry in request_ids:
            raw_entry = str(entry or "").strip()
            if not raw_entry or raw_entry in seen:
                continue
            seen.add(raw_entry)

            # Resolve tenant and req_id
            if is_admin and ":" in raw_entry:
                tp, req = raw_entry.split(":", 1)
            else:
                tp, req = tenant_prefix, raw_entry

            summary_key = f"ithqbot:trace:tenant:{tp}:task:{req}"
            try:
                raw = client.hgetall(summary_key)
            except Exception:
                raw = {}
            if not raw:
                continue
            task = self._normalize_trace_task(raw)
            updated_at_ms = _as_int(task.get("updated_at_ms"))

            # Additional Filters
            if normalized_account and not _match_filter(task.get("account_id"), normalized_account):
                continue
            if not _match_filter(task.get("bot_id"), normalized_bot):
                continue
            if not _match_filter(task.get("chat_id"), normalized_chat):
                continue
            if not _match_filter(task.get("client_id"), normalized_client):
                continue
            if not _match_filter(task.get("channel"), normalized_channel):
                continue
            if not _match_filter(task.get("status"), normalized_status):
                continue
            if isinstance(start_at, int) and updated_at_ms < start_at:
                continue
            if isinstance(end_at, int) and updated_at_ms > end_at:
                continue
            if normalized_keyword and normalized_keyword not in str(task.get("search_text") or "").lower():
                continue

            tasks.append(task)
            if len(tasks) >= normalized_limit:
                break
        return tasks

    def get_trace_task(
        self,
        request_msg_id: str,
        tenant_id: str | None = None,
        is_admin: bool = False,
        requester_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        resolved_tenant_id = self._resolve_trace_tenant_id(tenant_id)
        is_admin = self._resolve_is_admin(is_admin, requester_account_id)
        client = self._client_or_none()
        req = str(request_msg_id or "").strip()
        if not req:
            return None

        if client is None:
            with self._lock:
                task = self._memory_trace_tasks.get(req)
                if not isinstance(task, dict):
                    return None
                if not is_admin and resolved_tenant_id and not _match_filter(task.get("tenant_id"), resolved_tenant_id):
                    return None
                return dict(task)

        tp = _safe_key_part(resolved_tenant_id, "default")

        # If admin, we might not know the tenant. For a single task fetch,
        # we usually need the context. In a premium system, we'd have a
        # global lookup table: tid = client.get(f"ithqbot:trace:lookup:{req}")
        # For now, we assume tenant_id is either provided or we try 'default'.
        summary_key = f"ithqbot:trace:tenant:{tp}:task:{req}"
        try:
            raw = client.hgetall(summary_key)
            # Fallback to default tenant if not found
            if not raw and tp != "default":
                raw = client.hgetall(f"ithqbot:trace:tenant:default:task:{req}")
        except Exception:
            raw = {}

        if not raw and is_admin:
            # Admin may not know tenant_id; try to resolve from global indices.
            resolved_tp = self._resolve_tenant_prefix_for_req_id(req)
            if resolved_tp and resolved_tp != tp:
                try:
                    raw = client.hgetall(f"ithqbot:trace:tenant:{resolved_tp}:task:{req}")
                except Exception:
                    raw = {}

        if not raw:
            return None
        return self._normalize_trace_task(raw)

    def _resolve_tenant_prefix_for_req_id(self, req_id: str) -> str | None:
        """Best-effort lookup of tenant prefix for a request id.

        We store `tenant:req_id` entries in global recent/account indices. When an
        admin requests a single trace task without tenant_id, we scan a bounded
        window to find the tenant prefix. This keeps runtime acceptable while
        avoiding a new persistence structure.
        """
        client = self._client_or_none()
        if client is None:
            return None
        req = str(req_id or "").strip()
        if not req:
            return None

        try:
            mapped = client.get(f"ithqbot:trace:lookup:{req}")
        except Exception:
            mapped = None
        if mapped:
            return _safe_key_part(str(mapped), "default")

        scan_window = 500
        keys = ["ithqbot:trace:index:recent"]
        try:
            values = client.lrange(keys[0], 0, scan_window - 1)
        except Exception:
            values = []
        for entry in values:
            raw = str(entry or "").strip()
            if not raw or ":" not in raw:
                continue
            tp, candidate = raw.split(":", 1)
            if candidate == req:
                return _safe_key_part(tp, "default")
        return None

    def get_trace_task_events(
        self,
        request_msg_id: str,
        tenant_id: str | None = None,
        limit: int = 500,
        is_admin: bool = False,
        requester_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        resolved_tenant_id = self._resolve_trace_tenant_id(tenant_id)
        is_admin = self._resolve_is_admin(is_admin, requester_account_id)
        client = self._client_or_none()
        req = str(request_msg_id or "").strip()
        if not req:
            return []

        tp = _safe_key_part(resolved_tenant_id, "default")
        if is_admin and not resolved_tenant_id:
            resolved_tp = self._resolve_tenant_prefix_for_req_id(req)
            if resolved_tp:
                tp = resolved_tp
        events_key = f"ithqbot:trace:tenant:{tp}:task:{req}:events"
        normalized_limit = _normalize_limit(limit, upper=1000)

        if client is None:
            if not is_admin and resolved_tenant_id:
                task = self.get_trace_task(
                    req,
                    tenant_id=resolved_tenant_id,
                    is_admin=False,
                    requester_account_id=requester_account_id,
                )
                if task is None:
                    return []
            with self._lock:
                rows = list(self._memory_trace_events.get(req) or [])
            return rows[-normalized_limit:]
        try:
            rows = client.lrange(events_key, 0, normalized_limit - 1)
            # Fallback to default tenant if empty
            if not rows and tp != "default":
                rows = client.lrange(f"ithqbot:trace:tenant:default:task:{req}:events", 0, normalized_limit - 1)
        except Exception:
            rows = []
        events: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                events.append(json.loads(row))
            except Exception:
                continue
        return events

    def get_trace_tree(
        self,
        request_msg_id: str,
        tenant_id: str | None = None,
        is_admin: bool = False,
        requester_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Returns a hierarchical trace tree for a given request."""
        task = self.get_trace_task(
            request_msg_id,
            tenant_id=tenant_id,
            is_admin=is_admin,
            requester_account_id=requester_account_id,
        )

        events = self.get_trace_task_events(
            request_msg_id,
            tenant_id=tenant_id,
            limit=2000,
            is_admin=is_admin,
            requester_account_id=requester_account_id,
        )
        if not events:
            return {**(task or {"request_msg_id": request_msg_id}), "tree": []}

        # Build tree
        sorted_events = sorted(events, key=lambda x: x.get("ts_ms", 0))
        # Two-pass tree construction to handle cross-references reliably
        nodes: dict[str, dict[str, Any]] = {}

        # Pass 1: Initialize all unique nodes and merge events sharing the same run_id
        for ev in sorted_events:
            details = ev.get("details") if isinstance(ev.get("details"), dict) else {}
            event_id = _trace_tree_event_id(ev)

            if event_id in nodes:
                existing = nodes[event_id]
                if ev.get("status"):
                    existing["status"] = ev.get("status")
                if ev.get("duration_ms"):
                    existing["duration"] = ev.get("duration_ms")
                if ev.get("event_name"):
                    existing["name"] = ev.get("event_name")
                if ev.get("component"):
                    existing["component"] = ev.get("component")
                parent_id = _trace_tree_parent_id(ev)
                if parent_id:
                    existing["parent_id"] = parent_id
                if details:
                    existing["details"].update(details)
                continue

            nodes[event_id] = {
                "id": event_id,
                "name": ev.get("event_name"),
                "component": ev.get("component"),
                "status": ev.get("status"),
                "start_time": ev.get("ts_ms"),
                "duration": ev.get("duration_ms"),
                "parent_id": _trace_tree_parent_id(ev),
                "details": details,
                "children": [],
            }

        # Pass 2: Link parents and children
        root_nodes = []
        for node in nodes.values():
            parent_id = node.get("parent_id")
            if parent_id and parent_id in nodes:
                nodes[parent_id]["children"].append(node)
            else:
                root_nodes.append(node)

        return {
            **task,
            "tree": sorted(root_nodes, key=lambda item: item.get("start_time") or 0),
        }

    def get_trace_graph(
        self,
        request_msg_id: str,
        tenant_id: str | None = None,
        is_admin: bool = False,
        requester_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        task = self.get_trace_task(
            request_msg_id,
            tenant_id=tenant_id,
            is_admin=is_admin,
            requester_account_id=requester_account_id,
        )
        events = self.get_trace_task_events(
            request_msg_id,
            tenant_id=tenant_id,
            limit=2000,
            is_admin=is_admin,
            requester_account_id=requester_account_id,
        )
        return _build_trace_graph_payload(task, request_msg_id, events)

    def get_trace_summary(
        self,
        *,
        account_id: str | None = None,
        tenant_id: str | None = None,
        is_admin: bool = False,
        requester_account_id: str | None = None,
        chat_id: str | None = None,
        client_id: str | None = None,
        bot_id: str | None = None,
        channel: str | None = None,
        status: str | None = None,
        start_at: int | None = None,
        end_at: int | None = None,
        keyword: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        tasks = self.get_trace_tasks(
            account_id=account_id,
            tenant_id=tenant_id,
            is_admin=is_admin,
            requester_account_id=requester_account_id,
            chat_id=chat_id,
            client_id=client_id,
            bot_id=bot_id,
            channel=channel,
            status=status,
            start_at=start_at,
            end_at=end_at,
            keyword=keyword,
            limit=limit,
        )
        return _build_trace_summary_payload(tasks)

    def get_account_llm_stats(self, account_id: str, bot_id: str | None = None) -> dict[str, dict[str, Any]]:
        client = self._client_or_none()
        if client is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        bot_part = _safe_key_part(bot_id, "default") if bot_id else None

        tenant_id = context.get_runtime_context().get("tenant_id")
        tp = _safe_key_part(tenant_id, "default") if tenant_id else None

        for source in sorted(_SOURCES):
            keys: list[str] = []
            if tp:
                if bot_part:
                    keys.append(f"ithqbot:trace:tenant:{tp}:llm:stats:{account_id}:bot:{bot_part}:{source}")
                else:
                    keys.append(f"ithqbot:trace:tenant:{tp}:llm:stats:{account_id}:{source}")
            if bot_part:
                keys.append(f"ithqbot:trace:llm:stats:{account_id}:bot:{bot_part}:{source}")
            else:
                keys.append(f"ithqbot:trace:llm:stats:{account_id}:{source}")
            raw: dict[str, Any] = {}
            for key in keys:
                try:
                    candidate = client.hgetall(key)
                except Exception:
                    candidate = {}
                if candidate:
                    raw = candidate
                    break
            normalized: dict[str, Any] = {}
            for k, v in raw.items():
                if k.endswith("_count") or k.endswith("_tokens"):
                    try:
                        normalized[k] = int(v)
                    except Exception:
                        normalized[k] = 0
                else:
                    normalized[k] = v
            result[source] = normalized
        return result

    def get_recent_audit_logs(
        self,
        account_id: str,
        limit: int = 100,
        bot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        client = self._client_or_none()
        if client is None:
            return []

        tenant_id = context.get_runtime_context().get("tenant_id")
        tp = _safe_key_part(tenant_id, "default") if tenant_id else None
        keys: list[str] = []
        if tp:
            if bot_id:
                keys.append(f"ithqbot:trace:tenant:{tp}:audit:{account_id}:bot:{_safe_key_part(bot_id, 'default')}")
            else:
                keys.append(f"ithqbot:trace:tenant:{tp}:audit:{account_id}")
        keys.append(f"ithqbot:trace:audit:global:{account_id}")
        limit = max(1, min(limit, 500))
        rows: list[str] = []
        for key in keys:
            try:
                candidate = client.lrange(key, 0, limit - 1)
            except Exception:
                candidate = []
            if candidate:
                rows = candidate
                break
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                event = json.loads(row)
            except Exception:
                continue
            if bot_id and str(event.get("bot_id") or "").strip() != bot_id:
                continue
            events.append(event)
        return events


_STORE = ObservabilityStore()


def configure_observability(redis_uri: str | None, config: ObservabilityConfig | None = None) -> None:
    _STORE.configure(redis_uri, config=config)


def audit_call(
    *,
    call_type: str,
    name: str,
    phase: str,
    input_data: Any = None,
    output_data: Any = None,
    interaction_data: Any = None,
    extra: dict[str, Any] | None = None,
) -> None:
    _STORE.record_audit(
        call_type=call_type,
        name=name,
        phase=phase,
        input_data=input_data,
        output_data=output_data,
        interaction_data=interaction_data,
        extra=extra,
    )


def record_llm_usage(
    *,
    source: str,
    model: str | None,
    usage: dict[str, Any] | None,
    finish_reason: str,
    component: str | None = None,
    duration_ms: int | None = None,
) -> None:
    _STORE.record_llm_usage(
        source=source,
        model=model,
        usage=usage,
        finish_reason=finish_reason,
        component=component,
        duration_ms=duration_ms,
    )


def get_account_llm_stats(account_id: str, bot_id: str | None = None) -> dict[str, dict[str, Any]]:
    return _STORE.get_account_llm_stats(account_id, bot_id=bot_id)


def get_recent_audit_logs(
    account_id: str,
    limit: int = 100,
    bot_id: str | None = None,
) -> list[dict[str, Any]]:
    return _STORE.get_recent_audit_logs(account_id, limit=limit, bot_id=bot_id)


def build_routing_observability_payload(
    routing: dict[str, Any] | None = None,
    *,
    route_profile: Any = None,
    requested_purpose: str | None = None,
    selected_purpose: str | None = None,
    current_purpose: str | None = None,
    initial_model: str | None = None,
    current_model: str | None = None,
    final_model: str | None = None,
    initial_tier: str | None = None,
    current_tier: str | None = None,
    source: str | None = None,
    fallback_models: list[str] | None = None,
    fallback_count: int | None = None,
    fallback_used: bool | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    task: str | None = None,
    skill_name: str | None = None,
) -> dict[str, Any]:
    payload = dict(routing or {})
    profile_purpose = getattr(route_profile, "purpose", None)
    profile_purpose_value = getattr(profile_purpose, "value", profile_purpose)
    profile_model = getattr(route_profile, "active_model", None) or getattr(route_profile, "model", None)
    profile_tier = getattr(route_profile, "tier", None)
    profile_fallbacks = getattr(route_profile, "fallback_models", None)
    profile_source = getattr(route_profile, "source", None)
    profile_max_tokens = getattr(route_profile, "max_tokens", None)
    profile_reasoning_effort = getattr(route_profile, "reasoning_effort", None)
    resolved_selected = str(
        selected_purpose
        or payload.get("selected_purpose")
        or payload.get("purpose")
        or (profile_purpose_value or "")
    ).strip()
    resolved_requested = str(
        requested_purpose
        or payload.get("requested_purpose")
        or resolved_selected
    ).strip()
    resolved_current = str(
        current_purpose
        or payload.get("current_purpose")
        or resolved_selected
    ).strip()
    resolved_initial_model = str(
        initial_model
        or payload.get("initial_model")
        or payload.get("model")
        or profile_model
        or ""
    ).strip()
    resolved_current_model = str(
        current_model
        or payload.get("current_model")
        or payload.get("final_model")
        or profile_model
        or resolved_initial_model
    ).strip()
    resolved_final_model = str(
        final_model
        or payload.get("final_model")
        or resolved_current_model
        or resolved_initial_model
    ).strip()
    resolved_initial_tier = initial_tier or payload.get("initial_tier") or payload.get("tier") or profile_tier
    resolved_current_tier = current_tier or payload.get("current_tier") or profile_tier or resolved_initial_tier
    resolved_fallbacks = fallback_models
    if resolved_fallbacks is None:
        raw_fallbacks = payload.get("fallback_models")
        if isinstance(raw_fallbacks, list):
            resolved_fallbacks = [str(item) for item in raw_fallbacks if str(item).strip()]
        elif isinstance(profile_fallbacks, list):
            resolved_fallbacks = [str(item) for item in profile_fallbacks if str(item).strip()]
        else:
            resolved_fallbacks = []
    return {
        "source": str(source or payload.get("source") or profile_source or "").strip(),
        "task": str(task or payload.get("task") or "").strip(),
        "skill_name": str(skill_name or payload.get("skill_name") or "").strip(),
        "requested_purpose": resolved_requested,
        "selected_purpose": resolved_selected,
        "current_purpose": resolved_current,
        "purpose": resolved_selected,
        "initial_model": resolved_initial_model,
        "current_model": resolved_current_model,
        "final_model": resolved_final_model,
        "model": resolved_initial_model,
        "initial_tier": resolved_initial_tier,
        "current_tier": resolved_current_tier,
        "tier": resolved_initial_tier,
        "fallback_models": resolved_fallbacks,
        "fallback_count": int(fallback_count if fallback_count is not None else payload.get("fallback_count") or 0),
        "fallback_used": bool(payload.get("fallback_used") if fallback_used is None else fallback_used),
        "max_tokens": max_tokens if max_tokens is not None else payload.get("max_tokens", profile_max_tokens),
        "reasoning_effort": (
            reasoning_effort
            if reasoning_effort is not None
            else payload.get("reasoning_effort", profile_reasoning_effort)
        ),
        "success": payload.get("success"),
    }


def record_trace_event(
    *,
    event_name: str,
    phase: str = "point",
    status: str | None = None,
    component: str | None = None,
    source: str | None = None,
    duration_ms: int | None = None,
    details: Any = None,
    account_id: str | None = None,
    tenant_id: str | None = None,
    bot_id: str | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    client_id: str | None = None,
    request_msg_id: str | None = None,
    trace_id: str | None = None,
    parent_run_id: str | None = None,
    content_preview: str | None = None,
    event_type: str | None = None,
) -> None:
    _STORE.record_trace_event(
        event_name=event_name,
        phase=phase,
        status=status,
        component=component,
        source=source,
        duration_ms=duration_ms,
        details=details,
        account_id=account_id,
        tenant_id=tenant_id,
        bot_id=bot_id,
        channel=channel,
        chat_id=chat_id,
        client_id=client_id,
        request_msg_id=request_msg_id,
        trace_id=trace_id,
        parent_run_id=parent_run_id,
        content_preview=content_preview,
        event_type=event_type,
    )


def get_trace_tasks(
    *,
    account_id: str | None = None,
    tenant_id: str | None = None,
    is_admin: bool = False,
    requester_account_id: str | None = None,
    chat_id: str | None = None,
    client_id: str | None = None,
    bot_id: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    start_at: int | None = None,
    end_at: int | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return _STORE.get_trace_tasks(
        account_id=account_id,
        tenant_id=tenant_id,
        is_admin=is_admin,
        requester_account_id=requester_account_id,
        chat_id=chat_id,
        client_id=client_id,
        bot_id=bot_id,
        channel=channel,
        status=status,
        start_at=start_at,
        end_at=end_at,
        keyword=keyword,
        limit=limit,
    )


def get_trace_task(
    request_msg_id: str,
    tenant_id: str | None = None,
    is_admin: bool = False,
    requester_account_id: str | None = None,
) -> dict[str, Any] | None:
    return _STORE.get_trace_task(
        request_msg_id,
        tenant_id=tenant_id,
        is_admin=is_admin,
        requester_account_id=requester_account_id,
    )


def get_trace_task_events(
    request_msg_id: str,
    tenant_id: str | None = None,
    limit: int = 500,
    is_admin: bool = False,
    requester_account_id: str | None = None,
) -> list[dict[str, Any]]:
    return _STORE.get_trace_task_events(
        request_msg_id,
        tenant_id=tenant_id,
        limit=limit,
        is_admin=is_admin,
        requester_account_id=requester_account_id,
    )


def get_trace_tree(
    request_msg_id: str,
    tenant_id: str | None = None,
    is_admin: bool = False,
    requester_account_id: str | None = None,
) -> dict[str, Any] | None:
    return _STORE.get_trace_tree(
        request_msg_id,
        tenant_id=tenant_id,
        is_admin=is_admin,
        requester_account_id=requester_account_id,
    )


def get_trace_graph(
    request_msg_id: str,
    tenant_id: str | None = None,
    is_admin: bool = False,
    requester_account_id: str | None = None,
) -> dict[str, Any] | None:
    return _STORE.get_trace_graph(
        request_msg_id,
        tenant_id=tenant_id,
        is_admin=is_admin,
        requester_account_id=requester_account_id,
    )


def get_trace_summary(
    *,
    account_id: str | None = None,
    tenant_id: str | None = None,
    is_admin: bool = False,
    requester_account_id: str | None = None,
    chat_id: str | None = None,
    client_id: str | None = None,
    bot_id: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    start_at: int | None = None,
    end_at: int | None = None,
    keyword: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    return _STORE.get_trace_summary(
        account_id=account_id,
        tenant_id=tenant_id,
        is_admin=is_admin,
        requester_account_id=requester_account_id,
        chat_id=chat_id,
        client_id=client_id,
        bot_id=bot_id,
        channel=channel,
        status=status,
        start_at=start_at,
        end_at=end_at,
        keyword=keyword,
        limit=limit,
    )


def get_observability_status() -> dict[str, Any]:
    """Get the current health and status of the observability store."""
    client = _STORE._client_or_none()
    return {
        "healthy": _STORE.is_healthy(),
        "backend": "redis" if client is not None else "memory",
        "uri": _STORE._uri if _STORE._uri else None,
    }
