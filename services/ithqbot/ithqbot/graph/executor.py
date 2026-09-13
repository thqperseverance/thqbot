from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import replace
from typing import Any, Callable

from ithqbot.agent.skills.base import SkillContext
from ithqbot.observability import record_trace_event

from .exceptions import GraphExecutionError, InteractionRequired
from .models import Graph, GraphRun, Node
from .scheduler import get_ready_nodes, get_skippable_nodes, validate_graph


class GraphExecutor:
    def __init__(
        self,
        state_manager,
        skill_runner,
        graph_loader: Callable[[str], Graph] | None = None,
    ):
        self.state_manager = state_manager
        self.skill_runner = skill_runner
        self.graph_loader = graph_loader
        self._state_lock = asyncio.Lock()

    async def run(self, graph: Graph, run: GraphRun, skill_context: SkillContext):
        validate_graph(graph)
        self._hydrate_graph(graph, run)
        run.status = "running"
        self.state_manager.save_run(run)
        run_started_at = time.monotonic()
        self._record_graph_event(
            skill_context,
            event_name="graph.run.start",
            phase="start",
            status="running",
            details=self._graph_details(graph, run),
        )
        await self._emit_status_update(
            skill_context,
            "技能图开始执行",
            progress_percent=self._calculate_progress(graph),
            progress_kind="graph",
            progress_stage="graph_running",
            call_type="graph",
            status_details=self._graph_details(graph, run),
        )

        while True:
            skipped_nodes = get_skippable_nodes(graph, run)
            if skipped_nodes:
                for node in skipped_nodes:
                    node.status = "done"
                    node.output = {"status": "skipped", "data": {}}
                    run.node_results[node.id] = dict(node.output)
                    self.state_manager.save_node(run.run_id, node)
                    self._record_graph_event(
                        skill_context,
                        event_name="graph.node.skipped",
                        phase="point",
                        status="skipped",
                        component=node.skill,
                        details=self._node_details(graph, run, node),
                    )
                    await self._emit_status_update(
                        skill_context,
                        f"节点 {node.id} 已跳过",
                        progress_percent=self._calculate_progress(graph),
                        progress_kind="graph",
                        progress_stage="graph_running",
                        skill_name=node.skill,
                        call_type="graph",
                        status_details=self._node_details(graph, run, node),
                    )
                self.state_manager.save_run(run)

            ready_nodes = get_ready_nodes(graph, run)
            if not ready_nodes:
                break
            await self._execute_ready_nodes(graph, ready_nodes, run, skill_context)

        if any(node.status == "failed" for node in graph.nodes.values()):
            run.status = "failed"
        elif any(node.status == "waiting" for node in graph.nodes.values()):
            run.status = "waiting"
        else:
            run.status = "done"

        self.state_manager.save_run(run)
        final_message = {
            "done": "技能图执行完成",
            "waiting": "技能图等待用户输入",
            "failed": "技能图执行失败",
        }.get(run.status, "技能图执行结束")
        final_stage = "graph_waiting" if run.status == "waiting" else "graph_running"
        final_status = "ok" if run.status == "done" else run.status
        self._record_graph_event(
            skill_context,
            event_name=f"graph.run.{run.status}",
            phase="end",
            status=final_status,
            duration_ms=int((time.monotonic() - run_started_at) * 1000),
            details=self._graph_details(graph, run),
        )
        await self._emit_status_update(
            skill_context,
            final_message,
            progress_percent=100 if run.status == "done" else self._calculate_progress(graph),
            progress_kind="graph",
            progress_stage=final_stage,
            call_type="graph",
            status_details=self._graph_details(graph, run),
        )
        return run

    async def run_by_id(
        self,
        graph_id: str,
        run: GraphRun,
        skill_context: SkillContext,
    ) -> GraphRun:
        graph = self._load_graph(graph_id)
        return await self.run(graph, run, skill_context)

    async def resume(
        self,
        run_id: str,
        user_input: dict[str, Any],
        skill_context: SkillContext,
        graph: Graph | None = None,
    ) -> GraphRun:
        run = self.state_manager.load_run(run_id)
        if run is None:
            raise GraphExecutionError(f"Graph run not found: {run_id}")

        active_graph = graph or self._load_graph(run.graph_id)
        self._hydrate_graph(active_graph, run)
        run.state.update(user_input or {})

        for node in active_graph.nodes.values():
            if node.status == "waiting":
                node_context = self._build_node_context(skill_context, active_graph, run, node)
                node.status = "pending"
                self.state_manager.save_node(run.run_id, node)
                self._record_graph_event(
                    node_context,
                    event_name="graph.node.resume",
                    phase="point",
                    status="running",
                    component=node.skill,
                    details=self._node_details(active_graph, run, node),
                )

        self.state_manager.save_run(run)
        self._record_graph_event(
            skill_context,
            event_name="graph.run.resume",
            phase="point",
            status="running",
            details=self._graph_details(active_graph, run),
        )
        return await self.run(active_graph, run, skill_context)

    async def execute_node(
        self,
        graph: Graph,
        node: Node,
        run: GraphRun,
        skill_context: SkillContext,
    ) -> None:
        node.status = "running"
        self.state_manager.save_node(run.run_id, node)
        node_context = self._build_node_context(skill_context, graph, run, node)
        node_started_at = time.monotonic()
        self._record_graph_event(
            node_context,
            event_name="graph.node.start",
            phase="start",
            status="running",
            component=node.skill,
            details=self._node_details(graph, run, node),
        )
        await self._emit_status_update(
            node_context,
            f"开始执行节点 {node.id}",
            progress_percent=self._calculate_progress(graph),
            progress_kind="graph",
            progress_stage="graph_running",
            skill_name=node.skill,
            call_type="graph",
            status_details=self._node_details(graph, run, node),
        )

        try:
            input_data = self.build_input(node, run)
            result = await self._run_skill(node.skill, input_data, node_context)
            normalized_result = self._normalize_result(result)
            state_update = self._build_state_update(node, normalized_result)
            async with self._state_lock:
                node.output = normalized_result
                node.status = "done"
                run.state.update(state_update)
                run.node_results[node.id] = normalized_result
                self.state_manager.save_run(run)
                self.state_manager.save_node(run.run_id, node)
            self._record_graph_event(
                node_context,
                event_name="graph.node.done",
                phase="end",
                status="ok",
                component=node.skill,
                duration_ms=int((time.monotonic() - node_started_at) * 1000),
                details=self._node_details(
                    graph,
                    run,
                    node,
                    state_update=state_update,
                ),
            )
            await self._emit_status_update(
                node_context,
                f"节点 {node.id} 执行完成",
                progress_percent=self._calculate_progress(graph),
                progress_kind="graph",
                progress_stage="graph_running",
                skill_name=node.skill,
                call_type="graph",
                status_details=self._node_details(
                    graph,
                    run,
                    node,
                    state_update=state_update,
                ),
            )
        except InteractionRequired as exc:
            node.status = "waiting"
            run.status = "waiting"
            self.state_manager.save_node(run.run_id, node)
            self.state_manager.save_run(run)
            self._record_graph_event(
                node_context,
                event_name="graph.node.waiting",
                phase="end",
                status="waiting",
                component=node.skill,
                duration_ms=int((time.monotonic() - node_started_at) * 1000),
                details=self._node_details(
                    graph,
                    run,
                    node,
                    error=None,
                    interaction=exc.payload,
                ),
            )
            await self._emit_status_update(
                node_context,
                f"节点 {node.id} 等待用户输入",
                progress_percent=self._calculate_progress(graph),
                progress_kind="graph",
                progress_stage="graph_waiting",
                skill_name=node.skill,
                call_type="graph",
                status_details=self._node_details(
                    graph,
                    run,
                    node,
                    interaction=exc.payload,
                ),
            )
            await node_context.emit_interaction(exc.payload)
            raise
        except asyncio.CancelledError:
            if node.status == "running":
                node.status = "pending"
                self.state_manager.save_node(run.run_id, node)
            raise
        except Exception as exc:
            node.status = "failed"
            self.state_manager.save_node(run.run_id, node)
            self.state_manager.save_run(run)
            self._record_graph_event(
                node_context,
                event_name="graph.node.failed",
                phase="end",
                status="error",
                component=node.skill,
                duration_ms=int((time.monotonic() - node_started_at) * 1000),
                details=self._node_details(graph, run, node, error=str(exc)),
            )
            await self._emit_status_update(
                node_context,
                f"节点 {node.id} 执行失败",
                progress_percent=self._calculate_progress(graph),
                progress_kind="graph",
                progress_stage="graph_running",
                skill_name=node.skill,
                call_type="graph",
                status_details=self._node_details(graph, run, node, error=str(exc)),
            )
            raise

    def build_input(self, node: Node, run: GraphRun) -> dict[str, Any]:
        input_data = {**run.state, **node.input}
        for key, source in node.input_mapping.items():
            input_data[key] = self._resolve_mapping_value(source, run.state, input_data)
        return input_data

    async def _execute_ready_nodes(
        self,
        graph: Graph,
        ready_nodes: list[Node],
        run: GraphRun,
        skill_context: SkillContext,
    ) -> None:
        tasks = {
            asyncio.create_task(self.execute_node(graph, node, run, skill_context)): node.id
            for node in ready_nodes
        }

        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    exc = task.exception()
                    if exc is None:
                        continue
                    if isinstance(exc, InteractionRequired):
                        continue
                    for other_task in pending:
                        other_task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    raise exc
        finally:
            tasks.clear()

    async def _run_skill(
        self,
        skill_name: str,
        input_data: dict[str, Any],
        skill_context: SkillContext,
    ) -> Any:
        runner = self.skill_runner
        run_skill_method = getattr(runner, "run_skill", None)
        if callable(run_skill_method):
            return await run_skill_method(skill_name, input_data, skill_context)

        run_method = getattr(runner, "run", None)
        if callable(run_method):
            return await run_method(skill_name, input_data, skill_context)

        execute_method = getattr(runner, "execute", None)
        if callable(execute_method):
            return await execute_method(skill_name, input_data, context_obj=skill_context)

        raise GraphExecutionError("skill_runner must provide run() or execute()")

    def _normalize_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            return {"status": "success", "message": result, "data": {}}
        return {"status": "success", "message": "", "data": {"result": result}}

    def _build_state_update(self, node: Node, normalized_result: dict[str, Any]) -> dict[str, Any]:
        if not node.output_mapping:
            return dict(normalized_result.get("data", {}) or {})
        mapped: dict[str, Any] = {}
        for target_key, source in node.output_mapping.items():
            mapped[target_key] = self._resolve_mapping_value(
                source,
                normalized_result,
                normalized_result.get("data", {}) or {},
            )
        return mapped

    def _load_graph(self, graph_id: str) -> Graph:
        if self.graph_loader is None:
            raise GraphExecutionError("graph_loader is required for resume() without a graph argument")
        return self.graph_loader(graph_id)

    def _hydrate_graph(self, graph: Graph, run: GraphRun) -> None:
        persisted_nodes = self.state_manager.load_nodes(run.run_id)
        for node_id, saved_node in persisted_nodes.items():
            if node_id not in graph.nodes:
                continue
            graph_node = graph.nodes[node_id]
            graph_node.status = saved_node.status
            graph_node.output = dict(saved_node.output)
            if saved_node.status == "done":
                run.node_results[node_id] = dict(saved_node.output)

    def _build_node_context(
        self,
        skill_context: Any,
        graph: Graph,
        run: GraphRun,
        node: Node,
    ) -> SkillContext:
        metadata = dict(getattr(skill_context, "metadata", {}) or {})
        graph_metadata = (
            dict(metadata.get("graph", {}))
            if isinstance(metadata.get("graph"), dict)
            else {}
        )
        graph_metadata.update(
            {
                "graph_id": graph.graph_id,
                "run_id": run.run_id,
                "node_id": node.id,
            }
        )
        metadata["graph"] = graph_metadata
        if isinstance(skill_context, SkillContext):
            return replace(
                skill_context,
                skill_name=node.skill,
                parent_run_id=run.run_id,
                metadata=metadata,
            )
        cloned_context = copy.copy(skill_context)
        setattr(cloned_context, "skill_name", node.skill)
        setattr(cloned_context, "parent_run_id", run.run_id)
        setattr(cloned_context, "metadata", metadata)
        return cloned_context

    async def _emit_status_update(
        self,
        skill_context: SkillContext,
        message: str,
        *,
        progress_percent: int | None = None,
        progress_kind: str | None = None,
        progress_stage: str | None = None,
        skill_name: str | None = None,
        call_type: str | None = None,
        status_details: dict[str, Any] | None = None,
    ) -> None:
        emit_callback = getattr(skill_context, "emit_callback", None)
        if not callable(emit_callback):
            return
        kwargs: dict[str, Any] = {
            "progress_percent": progress_percent,
            "progress_kind": progress_kind,
            "progress_stage": progress_stage,
            "status_event": "processing",
            "skill_name": skill_name,
            "call_type": call_type,
            "status_details": status_details,
        }
        filtered_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        try:
            await emit_callback(message, **filtered_kwargs)
            return
        except TypeError:
            pass
        if progress_percent is not None and progress_stage:
            try:
                await emit_callback(progress_percent, progress_stage, message)
                return
            except TypeError:
                pass
        await emit_callback(message)

    def _record_graph_event(
        self,
        skill_context: SkillContext,
        *,
        event_name: str,
        phase: str,
        status: str,
        details: dict[str, Any],
        component: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        details_payload = dict(details)
        parent_run_id = getattr(skill_context, "parent_run_id", None)
        if parent_run_id and "parent_run_id" not in details_payload:
            details_payload["parent_run_id"] = parent_run_id
        record_trace_event(
            event_name=event_name,
            phase=phase,
            status=status,
            component=component or "graph_executor",
            source="skill",
            event_type="graph",
            duration_ms=duration_ms,
            details=details_payload,
            account_id=getattr(skill_context, "account_id", None),
            tenant_id=getattr(skill_context, "tenant_id", None),
            bot_id=getattr(skill_context, "bot_id", None),
            chat_id=getattr(skill_context, "chat_id", None),
            client_id=getattr(skill_context, "client_id", None),
            request_msg_id=getattr(skill_context, "request_msg_id", None),
            trace_id=getattr(skill_context, "trace_id", None),
            parent_run_id=parent_run_id,
        )

    def _graph_details(self, graph: Graph, run: GraphRun) -> dict[str, Any]:
        return {
            "graph_id": graph.graph_id,
            "run_id": run.run_id,
            "graph": {
                "graph_id": graph.graph_id,
                "run_id": run.run_id,
                "status": run.status,
                "node_count": len(graph.nodes),
                "completed_nodes": self._completed_nodes(graph),
                "failed_nodes": self._status_count(graph, "failed"),
                "waiting_nodes": self._status_count(graph, "waiting"),
                "running_nodes": self._status_count(graph, "running"),
                "pending_nodes": self._status_count(graph, "pending"),
                "skipped_nodes": self._skipped_nodes(graph),
                "current_node_ids": self._current_node_ids(graph),
                "topology": self._graph_topology(graph),
            }
        }

    def _node_details(
        self,
        graph: Graph,
        run: GraphRun,
        node: Node,
        *,
        state_update: dict[str, Any] | None = None,
        error: str | None = None,
        interaction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details = self._graph_details(graph, run)
        details["run_id"] = f"{run.run_id}:{node.id}"
        details["node_id"] = node.id
        details["parent_run_id"] = run.run_id
        details["node"] = {
            "node_id": node.id,
            "skill_name": node.skill,
            "status": node.status,
            "input_mapping_keys": sorted(node.input_mapping.keys()),
            "output_mapping_keys": sorted(node.output_mapping.keys()),
        }
        if state_update:
            details["node"]["state_update_keys"] = sorted(state_update.keys())
        if error:
            details["node"]["error"] = error
        if isinstance(interaction, dict):
            details["node"]["interaction"] = interaction
            details["node"]["waiting_for_input"] = True
        return details

    def _completed_nodes(self, graph: Graph) -> int:
        return sum(1 for node in graph.nodes.values() if node.status == "done")

    def _status_count(self, graph: Graph, status: str) -> int:
        return sum(1 for node in graph.nodes.values() if node.status == status)

    def _skipped_nodes(self, graph: Graph) -> int:
        return sum(
            1
            for node in graph.nodes.values()
            if isinstance(node.output, dict) and str(node.output.get("status") or "").strip().lower() == "skipped"
        )

    def _current_node_ids(self, graph: Graph) -> list[str]:
        return sorted(
            node.id
            for node in graph.nodes.values()
            if node.status in {"running", "waiting"}
        )

    def _graph_topology(self, graph: Graph) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "node_id": node.id,
                    "skill_name": node.skill,
                    "status": node.status,
                    "input_mapping_keys": sorted(node.input_mapping.keys()),
                    "output_mapping_keys": sorted(node.output_mapping.keys()),
                }
                for node in graph.nodes.values()
            ],
            "edges": [
                {
                    "from": edge.from_node,
                    "to": edge.to_node,
                    "condition": edge.condition,
                    "kind": "dependency",
                }
                for edge in graph.edges
            ],
        }

    def _calculate_progress(self, graph: Graph) -> int:
        total = max(len(graph.nodes), 1)
        completed = self._completed_nodes(graph)
        return min(100, int((completed / total) * 100))

    def _resolve_mapping_value(
        self,
        source: Any,
        state_scope: dict[str, Any],
        default_scope: dict[str, Any],
    ) -> Any:
        if not isinstance(source, str):
            return source
        reference = source.strip()
        if not reference:
            return None
        if reference.startswith("state."):
            return self._read_path(state_scope, reference[6:])
        if reference.startswith("state["):
            return self._read_bracket_path(state_scope, reference)
        if reference.startswith("data."):
            return self._read_path(default_scope, reference[5:])
        if reference.startswith("data["):
            return self._read_bracket_path(default_scope, reference)
        if reference.startswith("result."):
            return self._read_path(state_scope, reference[7:])
        return self._read_path(default_scope, reference)

    def _read_path(self, payload: Any, path: str) -> Any:
        current = payload
        for part in [segment for segment in path.split(".") if segment]:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _read_bracket_path(self, payload: Any, expr: str) -> Any:
        keys = [match.group(1) for match in __import__("re").finditer(r"\[['\"]([^'\"]+)['\"]\]", expr)]
        current = payload
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current
