from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .cancellation import CancellationSnapshot
from .human import HumanInteractionRequest, HumanInteractionResponse
from .state_machine import AgentRuntimeStateMachine, RuntimeStatus

_SCHEMA_VERSION = "agent-runtime-checkpoint/v1"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class RuntimeCursor:
    phase: str = "planner"
    iteration: int = 0
    node_id: str | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "iteration": self.iteration,
            "node_id": self.node_id,
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RuntimeCursor":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            phase=str(data.get("phase") or "planner"),
            iteration=max(0, int(data.get("iteration") or 0)),
            node_id=str(data.get("node_id") or "").strip() or None,
            tool_call_id=str(data.get("tool_call_id") or "").strip() or None,
        )


@dataclass(frozen=True)
class RuntimeCheckpoint:
    session_id: str
    run_id: str
    status: RuntimeStatus
    checkpoint_id: str = field(default_factory=lambda: f"chk-{uuid.uuid4()}")
    schema_version: str = _SCHEMA_VERSION
    version: int = 0
    created_at_ms: int = field(default_factory=_now_ms)
    updated_at_ms: int = field(default_factory=_now_ms)
    trace_id: str | None = None
    route_metadata: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    cursor: RuntimeCursor = field(default_factory=RuntimeCursor)
    tool_outputs: dict[str, Any] = field(default_factory=dict)
    memory_snapshot: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    retry_state: dict[str, Any] = field(default_factory=dict)
    cancellation: CancellationSnapshot | None = None
    pending_interaction: HumanInteractionRequest | None = None
    last_interaction_response: HumanInteractionResponse | None = None
    messages_snapshot: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "version": self.version,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "trace_id": self.trace_id,
            "route_metadata": dict(self.route_metadata),
            "plan": self.plan,
            "cursor": self.cursor.to_dict(),
            "tool_outputs": dict(self.tool_outputs),
            "memory_snapshot": dict(self.memory_snapshot),
            "variables": dict(self.variables),
            "retry_state": dict(self.retry_state),
            "cancellation": self.cancellation.to_dict() if self.cancellation is not None else None,
            "pending_interaction": self.pending_interaction.to_dict() if self.pending_interaction is not None else None,
            "last_interaction_response": self.last_interaction_response.to_dict() if self.last_interaction_response is not None else None,
            "messages_snapshot": list(self.messages_snapshot),
            "errors": list(self.errors),
            "metadata": dict(self.metadata),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, default=str)

    def with_status(self, status: RuntimeStatus, *, updated_at_ms: int | None = None) -> "RuntimeCheckpoint":
        transition = AgentRuntimeStateMachine.transition(self.status, status)
        if not transition.allowed:
            raise ValueError(f"invalid runtime transition: {self.status.value} -> {status.value}")
        return replace(self, status=status, updated_at_ms=updated_at_ms or _now_ms())

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RuntimeCheckpoint | None":
        if not isinstance(payload, dict):
            return None
        session_id = str(payload.get("session_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        if not session_id or not run_id:
            return None
        return cls(
            schema_version=str(payload.get("schema_version") or _SCHEMA_VERSION),
            checkpoint_id=str(payload.get("checkpoint_id") or f"chk-{uuid.uuid4()}"),
            session_id=session_id,
            run_id=run_id,
            status=AgentRuntimeStateMachine.normalize(payload.get("status") or RuntimeStatus.CREATED),
            version=max(0, int(payload.get("version") or 0)),
            created_at_ms=max(0, int(payload.get("created_at_ms") or _now_ms())),
            updated_at_ms=max(0, int(payload.get("updated_at_ms") or _now_ms())),
            trace_id=str(payload.get("trace_id") or "").strip() or None,
            route_metadata=dict(payload.get("route_metadata") or {}),
            plan=payload.get("plan") if isinstance(payload.get("plan"), dict) else None,
            cursor=RuntimeCursor.from_dict(payload.get("cursor")),
            tool_outputs=dict(payload.get("tool_outputs") or {}),
            memory_snapshot=dict(payload.get("memory_snapshot") or {}),
            variables=dict(payload.get("variables") or {}),
            retry_state=dict(payload.get("retry_state") or {}),
            cancellation=CancellationSnapshot.from_dict(payload.get("cancellation")) if payload.get("cancellation") else None,
            pending_interaction=HumanInteractionRequest.from_dict(payload.get("pending_interaction")),
            last_interaction_response=HumanInteractionResponse.from_dict(payload.get("last_interaction_response")),
            messages_snapshot=list(payload.get("messages_snapshot") or []),
            errors=list(payload.get("errors") or []),
            metadata=dict(payload.get("metadata") or {}),
        )

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        run_id: str,
        trace_id: str | None = None,
        route_metadata: dict[str, Any] | None = None,
        status: RuntimeStatus = RuntimeStatus.CREATED,
    ) -> "RuntimeCheckpoint":
        return cls(
            session_id=session_id,
            run_id=run_id,
            status=status,
            trace_id=trace_id,
            route_metadata=dict(route_metadata or {}),
        )


class BaseRuntimeCheckpointStore:
    def load(self, session_id: str) -> RuntimeCheckpoint | None:
        raise NotImplementedError

    def save(self, checkpoint: RuntimeCheckpoint, *, expected_version: int | None = None) -> RuntimeCheckpoint:
        raise NotImplementedError

    def delete(self, session_id: str) -> None:
        raise NotImplementedError

    def acquire_lease(self, session_id: str, owner_id: str, *, ttl_seconds: int = 60) -> bool:
        _ = (session_id, owner_id, ttl_seconds)
        return True

    def release_lease(self, session_id: str, owner_id: str) -> None:
        _ = (session_id, owner_id)


class FileRuntimeCheckpointStore(BaseRuntimeCheckpointStore):
    def __init__(self, workspace: Path):
        self._dir = workspace / "runtime_checkpoints"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_").replace(":", "_")
        return self._dir / f"{safe}.json"

    def load(self, session_id: str) -> RuntimeCheckpoint | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            return RuntimeCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def save(self, checkpoint: RuntimeCheckpoint, *, expected_version: int | None = None) -> RuntimeCheckpoint:
        current = self.load(checkpoint.session_id)
        if expected_version is not None and current is not None and current.version != expected_version:
            raise ValueError("runtime checkpoint version conflict")
        next_version = (current.version + 1) if current is not None else 1
        persisted = replace(checkpoint, version=next_version, updated_at_ms=_now_ms())
        self._path(checkpoint.session_id).write_text(persisted.to_json(), encoding="utf-8")
        return persisted

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            path.unlink()


class RedisRuntimeCheckpointStore(BaseRuntimeCheckpointStore):
    def __init__(self, uri: str, *, prefix: str = "ithqbot:runtime:checkpoint:"):
        import redis

        self.client = redis.Redis.from_url(uri, decode_responses=True)
        self.prefix = prefix

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}{session_id}"

    def _lease_key(self, session_id: str) -> str:
        return f"{self._key(session_id)}:lease"

    def load(self, session_id: str) -> RuntimeCheckpoint | None:
        payload = self.client.get(self._key(session_id))
        if not payload:
            return None
        try:
            return RuntimeCheckpoint.from_dict(json.loads(payload))
        except Exception:
            return None

    def save(self, checkpoint: RuntimeCheckpoint, *, expected_version: int | None = None) -> RuntimeCheckpoint:
        pipe = self.client.pipeline()
        key = self._key(checkpoint.session_id)
        while True:
            try:
                pipe.watch(key)
                current_payload = pipe.get(key)
                current = RuntimeCheckpoint.from_dict(json.loads(current_payload)) if current_payload else None
                if expected_version is not None and current is not None and current.version != expected_version:
                    raise ValueError("runtime checkpoint version conflict")
                next_version = (current.version + 1) if current is not None else 1
                persisted = replace(checkpoint, version=next_version, updated_at_ms=_now_ms())
                pipe.multi()
                pipe.set(key, persisted.to_json())
                pipe.execute()
                return persisted
            except self.client.WatchError:
                continue
            finally:
                pipe.reset()

    def delete(self, session_id: str) -> None:
        self.client.delete(self._key(session_id))

    def acquire_lease(self, session_id: str, owner_id: str, *, ttl_seconds: int = 60) -> bool:
        return bool(self.client.set(self._lease_key(session_id), owner_id, nx=True, ex=max(1, int(ttl_seconds))))

    def release_lease(self, session_id: str, owner_id: str) -> None:
        key = self._lease_key(session_id)
        if self.client.get(key) == owner_id:
            self.client.delete(key)


class PostgreSQLRuntimeCheckpointStore(BaseRuntimeCheckpointStore):
    def __init__(self, uri: str):
        import psycopg

        self._conn = psycopg.connect(uri, autocommit=True)
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runtime_checkpoints (
                    session_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    version BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    trace_id TEXT NULL,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runtime_leases (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_until TIMESTAMPTZ NOT NULL
                )
                """
            )

    def load(self, session_id: str) -> RuntimeCheckpoint | None:
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM agent_runtime_checkpoints WHERE session_id = %s",
                (session_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return RuntimeCheckpoint.from_dict(row[0] if isinstance(row[0], dict) else json.loads(row[0]))

    def save(self, checkpoint: RuntimeCheckpoint, *, expected_version: int | None = None) -> RuntimeCheckpoint:
        current = self.load(checkpoint.session_id)
        if expected_version is not None and current is not None and current.version != expected_version:
            raise ValueError("runtime checkpoint version conflict")
        next_version = (current.version + 1) if current is not None else 1
        persisted = replace(checkpoint, version=next_version, updated_at_ms=_now_ms())
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_runtime_checkpoints (session_id, run_id, version, status, trace_id, payload, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    version = EXCLUDED.version,
                    status = EXCLUDED.status,
                    trace_id = EXCLUDED.trace_id,
                    payload = EXCLUDED.payload,
                    updated_at = NOW()
                """,
                (
                    persisted.session_id,
                    persisted.run_id,
                    persisted.version,
                    persisted.status.value,
                    persisted.trace_id,
                    persisted.to_json(),
                ),
            )
        return persisted

    def delete(self, session_id: str) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute("DELETE FROM agent_runtime_checkpoints WHERE session_id = %s", (session_id,))

    def acquire_lease(self, session_id: str, owner_id: str, *, ttl_seconds: int = 60) -> bool:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_runtime_leases (session_id, owner_id, lease_until)
                VALUES (%s, %s, NOW() + (%s || ' seconds')::interval)
                ON CONFLICT (session_id) DO UPDATE SET
                    owner_id = EXCLUDED.owner_id,
                    lease_until = EXCLUDED.lease_until
                WHERE agent_runtime_leases.lease_until < NOW() OR agent_runtime_leases.owner_id = EXCLUDED.owner_id
                RETURNING owner_id
                """,
                (session_id, owner_id, max(1, int(ttl_seconds))),
            )
            row = cursor.fetchone()
        return bool(row and row[0] == owner_id)

    def release_lease(self, session_id: str, owner_id: str) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM agent_runtime_leases WHERE session_id = %s AND owner_id = %s",
                (session_id, owner_id),
            )


def create_runtime_checkpoint_store(workspace: Path, store_uri: str | None) -> BaseRuntimeCheckpointStore:
    uri = str(store_uri or "").strip()
    if not uri:
        return FileRuntimeCheckpointStore(workspace)
    scheme = uri.split("://", 1)[0].split("+", 1)[0].lower()
    if scheme in {"postgres", "postgresql"}:
        return PostgreSQLRuntimeCheckpointStore(uri)
    if scheme in {"redis", "rediss"}:
        return RedisRuntimeCheckpointStore(uri)
    return FileRuntimeCheckpointStore(workspace)
