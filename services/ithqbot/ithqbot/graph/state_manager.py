from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from copy import deepcopy

from .models import GraphRun, Node


class InMemoryStateManager:
    def __init__(self):
        self._runs: dict[str, GraphRun] = {}
        self._nodes: dict[str, dict[str, Node]] = {}

    def save_run(self, run: GraphRun) -> None:
        self._runs[run.run_id] = GraphRun(
            run_id=run.run_id,
            graph_id=run.graph_id,
            status=run.status,
            state=deepcopy(run.state),
            node_results=deepcopy(run.node_results),
        )

    def load_run(self, run_id: str) -> GraphRun | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        loaded = GraphRun(
            run_id=run.run_id,
            graph_id=run.graph_id,
            status=run.status,
            state=deepcopy(run.state),
            node_results=deepcopy(run.node_results),
        )
        for node_id, node in self.load_nodes(run_id).items():
            if node.status == "done":
                loaded.node_results[node_id] = deepcopy(node.output)
        return loaded

    def save_node(self, run_id: str, node: Node) -> None:
        run_nodes = self._nodes.setdefault(run_id, {})
        run_nodes[node.id] = Node(
            id=node.id,
            skill=node.skill,
            status=node.status,
            input=deepcopy(node.input),
            input_mapping=deepcopy(node.input_mapping),
            output_mapping=deepcopy(node.output_mapping),
            output=deepcopy(node.output),
        )

    def load_nodes(self, run_id: str) -> dict[str, Node]:
        return {
            node_id: Node(
                id=node.id,
                skill=node.skill,
                status=node.status,
                input=deepcopy(node.input),
                input_mapping=deepcopy(node.input_mapping),
                output_mapping=deepcopy(node.output_mapping),
                output=deepcopy(node.output),
            )
            for node_id, node in self._nodes.get(run_id, {}).items()
        }

    def close(self) -> None:
        return None


class StateManager:
    def __init__(self, db: Any):
        self._owns_connection = isinstance(db, str)
        self.db = self._connect(db) if self._owns_connection else db
        self._ensure_tables()

    def save_run(self, run: GraphRun) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO graph_runs (run_id, graph_id, status, state, created_at, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, NOW(), NOW())
                ON CONFLICT (run_id) DO UPDATE SET
                    graph_id = EXCLUDED.graph_id,
                    status = EXCLUDED.status,
                    state = EXCLUDED.state,
                    updated_at = NOW()
                """,
                (run.run_id, run.graph_id, run.status, self._encode_json(run.state)),
            )

    def load_run(self, run_id: str) -> GraphRun | None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, graph_id, status, state
                FROM graph_runs
                WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        run = GraphRun(
            run_id=str(row[0]),
            graph_id=str(row[1]),
            status=str(row[2]),
            state=self._decode_json(row[3]),
        )
        for node_id, node in self.load_nodes(run_id).items():
            if node.status == "done":
                run.node_results[node_id] = dict(node.output)
        return run

    def save_node(self, run_id: str, node: Node) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO graph_nodes (run_id, node_id, status, input, output, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, NOW())
                ON CONFLICT (run_id, node_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    input = EXCLUDED.input,
                    output = EXCLUDED.output,
                    updated_at = NOW()
                """,
                (
                    run_id,
                    node.id,
                    node.status,
                    self._encode_json(node.input),
                    self._encode_json(node.output),
                ),
            )

    def load_nodes(self, run_id: str) -> dict[str, Node]:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT node_id, status, input, output
                FROM graph_nodes
                WHERE run_id = %s
                """,
                (run_id,),
            )
            rows = cursor.fetchall()

        nodes: dict[str, Node] = {}
        for node_id, status, node_input, output in rows:
            nodes[str(node_id)] = Node(
                id=str(node_id),
                skill="",
                status=str(status),
                input=self._decode_json(node_input),
                output=self._decode_json(output),
            )
        return nodes

    def close(self) -> None:
        if self._owns_connection and hasattr(self.db, "close"):
            self.db.close()

    def _ensure_tables(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_runs (
                    run_id TEXT PRIMARY KEY,
                    graph_id TEXT,
                    status TEXT,
                    state JSONB,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    run_id TEXT,
                    node_id TEXT,
                    status TEXT,
                    input JSONB,
                    output JSONB,
                    updated_at TIMESTAMP,
                    PRIMARY KEY (run_id, node_id)
                )
                """
            )

    @contextmanager
    def _cursor(self):
        cursor = self.db.cursor()
        try:
            yield cursor
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def _connect(self, db: str):
        import psycopg

        return psycopg.connect(db, autocommit=True)

    def _encode_json(self, value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=False, default=str)

    def _decode_json(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, dict) else {}
        return dict(value)
