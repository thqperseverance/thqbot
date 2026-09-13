"""Cron service for scheduling agent tasks."""

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Coroutine, Protocol

from loguru import logger

from ithqbot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore
from ithqbot.utils.helpers import default_timezone, sanitize_connection_uri


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # Next interval from now
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter
            # Use caller-provided reference time for deterministic scheduling
            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else default_timezone()
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


def _job_to_record(job: CronJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": {
            "kind": job.schedule.kind,
            "atMs": job.schedule.at_ms,
            "everyMs": job.schedule.every_ms,
            "expr": job.schedule.expr,
            "tz": job.schedule.tz,
        },
        "payload": {
            "kind": job.payload.kind,
            "message": job.payload.message,
            "deliver": job.payload.deliver,
            "channel": job.payload.channel,
            "to": job.payload.to,
            "owner_id": job.payload.owner_id,
            "metadata": job.payload.metadata,
        },
        "state": {
            "nextRunAtMs": job.state.next_run_at_ms,
            "lastRunAtMs": job.state.last_run_at_ms,
            "lastStatus": job.state.last_status,
            "lastError": job.state.last_error,
        },
        "createdAtMs": job.created_at_ms,
        "updatedAtMs": job.updated_at_ms,
        "deleteAfterRun": job.delete_after_run,
    }


def _job_from_record(item: dict[str, Any]) -> CronJob:
    return CronJob(
        id=item["id"],
        name=item["name"],
        enabled=item.get("enabled", True),
        schedule=CronSchedule(
            kind=item["schedule"]["kind"],
            at_ms=item["schedule"].get("atMs"),
            every_ms=item["schedule"].get("everyMs"),
            expr=item["schedule"].get("expr"),
            tz=item["schedule"].get("tz"),
        ),
        payload=CronPayload(
            kind=item["payload"].get("kind", "agent_turn"),
            message=item["payload"].get("message", ""),
            deliver=item["payload"].get("deliver", False),
            channel=item["payload"].get("channel"),
            to=item["payload"].get("to"),
            owner_id=item["payload"].get("owner_id"),
            metadata=item["payload"].get("metadata", {}),
        ),
        state=CronJobState(
            next_run_at_ms=item.get("state", {}).get("nextRunAtMs"),
            last_run_at_ms=item.get("state", {}).get("lastRunAtMs"),
            last_status=item.get("state", {}).get("lastStatus"),
            last_error=item.get("state", {}).get("lastError"),
        ),
        created_at_ms=item.get("createdAtMs", 0),
        updated_at_ms=item.get("updatedAtMs", 0),
        delete_after_run=item.get("deleteAfterRun", False),
    )


def _job_scope(job: CronJob) -> tuple[str, str]:
    metadata = job.payload.metadata or {}
    tenant = metadata.get("tenant_id") if isinstance(metadata.get("tenant_id"), str) else None
    account = job.payload.owner_id or (
        metadata.get("account_id") if isinstance(metadata.get("account_id"), str) else None
    )
    return (tenant or "default", account or "default")


def _job_matches_scope(
    job: CronJob,
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
    bot_id: str | None = None,
) -> bool:
    metadata = job.payload.metadata or {}
    job_tenant_id = metadata.get("tenant_id") if isinstance(metadata.get("tenant_id"), str) else None
    job_bot_id = metadata.get("bot_id") if isinstance(metadata.get("bot_id"), str) else None

    if tenant_id and job_tenant_id != tenant_id:
        return False
    if owner_id and job.payload.owner_id != owner_id:
        return False
    if bot_id and job_bot_id != bot_id:
        return False
    return True


def _sanitize_store_uri(uri: str) -> str:
    return sanitize_connection_uri(uri)


class PostgreSQLCronStore:
    def __init__(self, uri: str):
        self.uri = uri
        self._driver_name = ""
        try:
            self._conn = self._connect(uri)
        except Exception as exc:
            safe_uri = _sanitize_store_uri(uri)
            raise RuntimeError(
                f"Failed to connect cron store using cronStoreUri={safe_uri}. "
                "Please check database host/port/user/password and database permissions."
            ) from exc
        self._max_attempts = 3
        self._base_delay_seconds = 0.2
        self._init_tables()

    def _connect(self, uri: str):
        import psycopg

        self._driver_name = "psycopg"
        return psycopg.connect(uri, autocommit=True)

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._connect(self.uri)

    def _run(self, operation: str, fn):
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return fn()
            except Exception as e:
                last_error = e
                if attempt >= self._max_attempts:
                    break
                delay = min(self._base_delay_seconds * (2 ** (attempt - 1)), 2.0)
                logger.warning(
                    "PostgreSQL cron store {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    e,
                )
                time.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error

    def _cursor(self):
        return self._conn.cursor()

    def _init_tables(self) -> None:
        def _op() -> None:
            cur = self._cursor()
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ithqbot_cron_jobs (
                        job_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        next_run_at_ms BIGINT,
                        updated_at_ms BIGINT NOT NULL,
                        job_data TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ithqbot_cron_jobs_scope_next
                    ON ithqbot_cron_jobs(tenant_id, account_id, next_run_at_ms)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ithqbot_cron_events (
                        seq BIGSERIAL PRIMARY KEY,
                        dedupe_key TEXT UNIQUE NOT NULL,
                        job_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        run_at_ms BIGINT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INT NOT NULL DEFAULT 0,
                        payload TEXT NOT NULL,
                        last_error TEXT,
                        claimed_by TEXT,
                        claimed_at_ms BIGINT,
                        created_at_ms BIGINT NOT NULL,
                        updated_at_ms BIGINT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ithqbot_cron_events_status_run
                    ON ithqbot_cron_events(status, run_at_ms, seq)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ithqbot_cron_events_scope_status_run
                    ON ithqbot_cron_events(tenant_id, account_id, status, run_at_ms, seq)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ithqbot_cron_events_scope_job
                    ON ithqbot_cron_events(tenant_id, account_id, job_id, created_at_ms)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ithqbot_cron_leases (
                        lease_name TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        expires_at_ms BIGINT NOT NULL,
                        updated_at_ms BIGINT NOT NULL
                    )
                    """
                )
            finally:
                cur.close()

        self._run("init_tables", _op)

    def load_store(self) -> CronStore:
        def _op() -> CronStore:
            cur = self._cursor()
            try:
                cur.execute("SELECT job_data FROM ithqbot_cron_jobs")
                rows = cur.fetchall() or []
            finally:
                cur.close()
            jobs = []
            for row in rows:
                try:
                    record = json.loads(row[0])
                    jobs.append(_job_from_record(record))
                except Exception:
                    continue
            return CronStore(jobs=jobs)

        return self._run("load_store", _op)

    def save_store(self, store: CronStore) -> None:
        def _op() -> None:
            cur = self._cursor()
            try:
                for job in store.jobs:
                    tenant_id, account_id = _job_scope(job)
                    cur.execute(
                        """
                        INSERT INTO ithqbot_cron_jobs (
                            job_id, tenant_id, account_id, enabled, next_run_at_ms, updated_at_ms, job_data
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (job_id)
                        DO UPDATE SET
                            tenant_id = EXCLUDED.tenant_id,
                            account_id = EXCLUDED.account_id,
                            enabled = EXCLUDED.enabled,
                            next_run_at_ms = EXCLUDED.next_run_at_ms,
                            updated_at_ms = EXCLUDED.updated_at_ms,
                            job_data = EXCLUDED.job_data
                        """,
                        (
                            job.id,
                            tenant_id,
                            account_id,
                            job.enabled,
                            job.state.next_run_at_ms,
                            job.updated_at_ms,
                            json.dumps(_job_to_record(job), ensure_ascii=False),
                        ),
                    )
            finally:
                cur.close()

        self._run("save_store", _op)

    def delete_job(self, job_id: str) -> None:
        def _op() -> None:
            cur = self._cursor()
            try:
                cur.execute("DELETE FROM ithqbot_cron_jobs WHERE job_id = %s", (job_id,))
                cur.execute(
                    "DELETE FROM ithqbot_cron_events WHERE job_id = %s AND status IN ('done', 'failed')",
                    (job_id,),
                )
            finally:
                cur.close()

        self._run("delete_job", _op)

    def acquire_lease(self, lease_name: str, worker_id: str, now_ms: int, ttl_ms: int) -> bool:
        expires = now_ms + ttl_ms

        def _op() -> bool:
            cur = self._cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO ithqbot_cron_leases (lease_name, owner_id, expires_at_ms, updated_at_ms)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (lease_name)
                    DO UPDATE SET
                        owner_id = EXCLUDED.owner_id,
                        expires_at_ms = EXCLUDED.expires_at_ms,
                        updated_at_ms = EXCLUDED.updated_at_ms
                    WHERE ithqbot_cron_leases.expires_at_ms < %s
                       OR ithqbot_cron_leases.owner_id = %s
                    RETURNING owner_id
                    """,
                    (lease_name, worker_id, expires, now_ms, now_ms, worker_id),
                )
                row = cur.fetchone()
            finally:
                cur.close()
            return bool(row and row[0] == worker_id)

        return self._run(f"acquire_lease[{lease_name}]", _op)

    def acquire_scheduler_lease(self, worker_id: str, now_ms: int, ttl_ms: int) -> bool:
        return self.acquire_lease("scheduler", worker_id, now_ms, ttl_ms)

    def enqueue_job_event(self, job: CronJob, scheduled_at_ms: int) -> None:
        tenant_id, account_id = _job_scope(job)
        now = _now_ms()
        dedupe_key = f"{job.id}:{scheduled_at_ms}"
        payload = json.dumps({"job": _job_to_record(job)}, ensure_ascii=False)

        def _op() -> None:
            cur = self._cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO ithqbot_cron_events (
                        dedupe_key, job_id, tenant_id, account_id, run_at_ms,
                        status, attempts, payload, created_at_ms, updated_at_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, 'pending', 0, %s, %s, %s)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    """,
                    (dedupe_key, job.id, tenant_id, account_id, scheduled_at_ms, payload, now, now),
                )
            finally:
                cur.close()

        self._run("enqueue_job_event", _op)

    def claim_due_event(self, worker_id: str, now_ms: int) -> dict[str, Any] | None:
        def _op():
            cur = self._cursor()
            try:
                cur.execute(
                    """
                    WITH picked AS (
                        SELECT seq
                        FROM ithqbot_cron_events
                        WHERE status = 'pending'
                          AND run_at_ms <= %s
                        ORDER BY run_at_ms ASC, seq ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE ithqbot_cron_events AS ev
                    SET status = 'processing',
                        attempts = ev.attempts + 1,
                        claimed_by = %s,
                        claimed_at_ms = %s,
                        updated_at_ms = %s
                    FROM picked
                    WHERE ev.seq = picked.seq
                    RETURNING ev.seq, ev.job_id, ev.payload, ev.attempts
                    """,
                    (now_ms, worker_id, now_ms, now_ms),
                )
                row = cur.fetchone()
            finally:
                cur.close()
            if not row:
                return None
            payload = {}
            try:
                payload = json.loads(row[2])
            except Exception:
                payload = {}
            return {"seq": row[0], "job_id": row[1], "payload": payload, "attempts": row[3]}

        return self._run("claim_due_event", _op)

    def complete_event(self, event_seq: int, *, ok: bool, error: str | None, attempts: int) -> None:
        now = _now_ms()
        if ok:
            def _op_ok() -> None:
                cur = self._cursor()
                try:
                    cur.execute(
                        """
                        UPDATE ithqbot_cron_events
                        SET status = 'done',
                            last_error = NULL,
                            updated_at_ms = %s
                        WHERE seq = %s
                        """,
                        (now, event_seq),
                    )
                finally:
                    cur.close()

            self._run("complete_event_ok", _op_ok)
            return

        if attempts >= 5:
            def _op_dead() -> None:
                cur = self._cursor()
                try:
                    cur.execute(
                        """
                        UPDATE ithqbot_cron_events
                        SET status = 'dead',
                            last_error = %s,
                            updated_at_ms = %s
                        WHERE seq = %s
                        """,
                        (error or "", now, event_seq),
                    )
                finally:
                    cur.close()

            self._run("complete_event_dead", _op_dead)
            return

        backoff_ms = min(60_000, (2 ** max(0, attempts - 1)) * 1_000)
        next_run = now + backoff_ms

        def _op_retry() -> None:
            cur = self._cursor()
            try:
                cur.execute(
                    """
                    UPDATE ithqbot_cron_events
                    SET status = 'pending',
                        run_at_ms = %s,
                        last_error = %s,
                        claimed_by = NULL,
                        claimed_at_ms = NULL,
                        updated_at_ms = %s
                    WHERE seq = %s
                    """,
                    (next_run, error or "", now, event_seq),
                )
            finally:
                cur.close()

        self._run("complete_event_retry", _op_retry)


class ExternalCronStore(Protocol):
    def load_store(self) -> CronStore: ...
    def save_store(self, store: CronStore) -> None: ...
    def delete_job(self, job_id: str) -> None: ...
    def acquire_lease(self, lease_name: str, worker_id: str, now_ms: int, ttl_ms: int) -> bool: ...
    def acquire_scheduler_lease(self, worker_id: str, now_ms: int, ttl_ms: int) -> bool: ...
    def enqueue_job_event(self, job: CronJob, scheduled_at_ms: int) -> None: ...
    def claim_due_event(self, worker_id: str, now_ms: int) -> dict[str, Any] | None: ...
    def complete_event(self, event_seq: int, *, ok: bool, error: str | None, attempts: int) -> None: ...


_CRON_STORE_FACTORIES: dict[str, Callable[[str], ExternalCronStore]] = {
    "postgresql": lambda uri: PostgreSQLCronStore(uri),
    "postgres": lambda uri: PostgreSQLCronStore(uri),
}


def create_external_cron_store(
    store_uri: str | None,
    *,
    require_external_store: bool = False,
) -> ExternalCronStore | None:
    if not store_uri:
        if require_external_store:
            raise ValueError("Cron service requires external store, but cronStoreUri is not configured")
        return None

    raw_scheme = (urlparse(store_uri).scheme or "").lower()
    scheme = raw_scheme.split("+", 1)[0]
    factory = _CRON_STORE_FACTORIES.get(scheme)
    if factory:
        store = factory(store_uri)
        logger.info("Cron service using external store scheme: {}", scheme)
        return store
    raise ValueError(
        f"Unsupported cron store URI scheme: {scheme} ({sanitize_connection_uri(store_uri)})"
    )


def register_cron_store_backend(
    scheme: str,
    factory: Callable[[str], ExternalCronStore],
) -> None:
    _CRON_STORE_FACTORIES[scheme] = factory


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path | None,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        store_uri: str | None = None,
        require_external_store: bool = False,
    ):
        self.store_path = store_path
        self.on_job = on_job
        self.store_uri = store_uri
        self.require_external_store = require_external_store
        self._store: CronStore | None = None
        self._last_mtime: float = 0.0
        self._timer_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._running = False
        self._instance_id = str(uuid.uuid4())
        self._lease_ttl_ms = 5000
        self._external_store = create_external_cron_store(
            store_uri,
            require_external_store=require_external_store,
        )

    @property
    def external_store(self) -> ExternalCronStore | None:
        return self._external_store

    def _load_store(self) -> CronStore:
        """Load jobs from disk. Reloads automatically if file was modified externally."""
        if self._external_store:
            self._store = self._external_store.load_store()
            return self._store
        if self.store_path is None:
            raise ValueError("Cron file store is disabled: cronStoreUri must be configured")
        if self._store and self.store_path.exists():
            mtime = self.store_path.stat().st_mtime
            if mtime != self._last_mtime:
                logger.info("Cron: jobs.json modified externally, reloading")
                self._store = None
        if self._store:
            return self._store

        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                jobs = []
                for j in data.get("jobs", []):
                    jobs.append(CronJob(
                        id=j["id"],
                        name=j["name"],
                        enabled=j.get("enabled", True),
                        schedule=CronSchedule(
                            kind=j["schedule"]["kind"],
                            at_ms=j["schedule"].get("atMs"),
                            every_ms=j["schedule"].get("everyMs"),
                            expr=j["schedule"].get("expr"),
                            tz=j["schedule"].get("tz"),
                        ),
                        payload=CronPayload(
                            kind=j["payload"].get("kind", "agent_turn"),
                            message=j["payload"].get("message", ""),
                            deliver=j["payload"].get("deliver", False),
                            channel=j["payload"].get("channel"),
                            to=j["payload"].get("to"),
                            owner_id=j["payload"].get("owner_id"),
                            metadata=j["payload"].get("metadata", {}),
                        ),
                        state=CronJobState(
                            next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                            last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                            last_status=j.get("state", {}).get("lastStatus"),
                            last_error=j.get("state", {}).get("lastError"),
                        ),
                        created_at_ms=j.get("createdAtMs", 0),
                        updated_at_ms=j.get("updatedAtMs", 0),
                        delete_after_run=j.get("deleteAfterRun", False),
                    ))
                self._store = CronStore(jobs=jobs)
            except Exception as e:
                logger.warning("Failed to load cron store: {}", e)
                self._store = CronStore()
        else:
            self._store = CronStore()

        return self._store

    def _save_store(self) -> None:
        """Save jobs to disk."""
        if not self._store:
            return
        if self._external_store:
            self._external_store.save_store(self._store)
            return
        if self.store_path is None:
            raise ValueError("Cron file store is disabled: cronStoreUri must be configured")

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                        "owner_id": j.payload.owner_id,
                        "metadata": j.payload.metadata,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                }
                for j in self._store.jobs
            ]
        }

        self.store_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._last_mtime = self.store_path.stat().st_mtime
    
    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._reconcile_missed_one_time_jobs()
        self._save_store()
        self._arm_timer()
        if self._external_store:
            self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("Cron service started with {} jobs", len(self._store.jobs if self._store else []))

    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                next_run = _compute_next_run(job.schedule, now)
                if job.schedule.kind == "at" and next_run is None and job.state.last_run_at_ms is None:
                    continue
                job.state.next_run_at_ms = next_run

    def _reconcile_missed_one_time_jobs(self) -> bool:
        """Handle one-time jobs that were missed while the scheduler was offline.

        Jobs within a grace period are rescheduled for immediate execution;
        jobs beyond the grace period are marked as skipped.
        """
        if not self._store:
            return False
        now = _now_ms()
        grace_period_ms = 5 * 60 * 1000
        changed = False
        for job in self._store.jobs:
            if not job.enabled or job.schedule.kind != "at":
                continue
            if job.state.last_run_at_ms is not None:
                continue
            at_ms = job.schedule.at_ms
            if not at_ms or at_ms > now:
                continue
            if now - at_ms < grace_period_ms:
                job.state.next_run_at_ms = now
                job.updated_at_ms = now
                changed = True
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
                if job.state.last_status is None:
                    job.state.last_status = "skipped"
                if not job.state.last_error:
                    job.state.last_error = "Missed scheduled run while cron service was unavailable."
                job.updated_at_ms = now
                changed = True
        return changed

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs
                 if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()

        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return

        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        self._load_store()
        if not self._store:
            return

        now = _now_ms()
        if self._external_store:
            has_lease = self._external_store.acquire_scheduler_lease(
                worker_id=self._instance_id,
                now_ms=now,
                ttl_ms=self._lease_ttl_ms,
            )
            if not has_lease:
                self._arm_timer()
                return

        due_jobs = [
            j for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
        ]

        if self._external_store:
            for job in due_jobs:
                scheduled_at = int(job.state.next_run_at_ms or now)
                self._external_store.enqueue_job_event(job, scheduled_at)
                if job.schedule.kind == "at":
                    if job.delete_after_run:
                        self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
                    else:
                        job.enabled = False
                        job.state.next_run_at_ms = None
                else:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
                job.updated_at_ms = _now_ms()
            self._save_store()
            self._arm_timer()
            return

        for job in due_jobs:
            await self._execute_job(job)

        self._save_store()
        self._arm_timer()

    async def _worker_loop(self) -> None:
        while self._running and self._external_store:
            event = None
            try:
                event = self._external_store.claim_due_event(self._instance_id, _now_ms())
            except Exception as e:
                logger.warning("Cron worker claim failed: {}", e)
            if not event:
                await asyncio.sleep(0.5)
                continue

            payload = event.get("payload") or {}
            record = payload.get("job")
            if not isinstance(record, dict):
                self._external_store.complete_event(
                    event_seq=event["seq"],
                    ok=False,
                    error="invalid job payload",
                    attempts=int(event.get("attempts") or 1),
                )
                continue
            job = _job_from_record(record)

            status, error, started_at = await self._execute_job_callback(job)
            try:
                self._store = self._load_store()
                for existing in self._store.jobs:
                    if existing.id == job.id:
                        existing.state.last_status = status
                        existing.state.last_error = error
                        existing.state.last_run_at_ms = started_at
                        existing.updated_at_ms = _now_ms()
                        break
                if job.schedule.kind == "at" and job.delete_after_run and status == "ok":
                    self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
                    self._external_store.delete_job(job.id)
                self._save_store()
            except Exception as e:
                logger.warning("Cron worker state update failed: {}", e)

            self._external_store.complete_event(
                event_seq=event["seq"],
                ok=(status == "ok"),
                error=error,
                attempts=int(event.get("attempts") or 1),
            )

    async def _execute_job_callback(self, job: CronJob) -> tuple[str, str | None, int]:
        start_ms = _now_ms()

        # Distributed execution guard
        if self._external_store:
            # We use next_run_at_ms as part of the lock key to ensure it's specific to this run.
            # If next_run_at_ms is missing, we use a rounded start_ms.
            run_key = job.state.next_run_at_ms or (start_ms // 1000 * 1000)
            lock_key = f"exec_lock:{job.id}:{run_key}"
            # 5-minute lease to prevent multiple executions even if the first one hangs or takes time.
            if not self._external_store.acquire_lease(lock_key, self._instance_id, start_ms, 300000):
                logger.info("Cron: job '{}' ({}) already being executed by another instance (key={})",
                            job.name, job.id, lock_key)
                return "skipped", "Already running elsewhere", start_ms

        logger.info("Cron: executing job '{}' ({})", job.name, job.id)
        try:
            if self.on_job:
                await self.on_job(job)
            logger.info("Cron: job '{}' completed", job.name)
            return "ok", None, start_ms
        except Exception as e:
            logger.error("Cron: job '{}' failed: {}", job.name, e)
            return "error", str(e), start_ms

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        status, error, start_ms = await self._execute_job_callback(job)
        job.state.last_status = status
        job.state.last_error = error

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()

        # Handle one-shot jobs
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            # Compute next run
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

    # ========== Public API ==========

    def list_jobs(
        self,
        include_disabled: bool = False,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        bot_id: str | None = None,
    ) -> list[CronJob]:
        """List all jobs, optionally filtered by tenant, owner, and bot."""
        store = self._load_store()
        if self._reconcile_missed_one_time_jobs():
            self._save_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]

        if tenant_id or owner_id or bot_id:
            jobs = [
                j
                for j in jobs
                if _job_matches_scope(j, tenant_id=tenant_id, owner_id=owner_id, bot_id=bot_id)
            ]

        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float("inf"))

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        owner_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """Add a new job."""
        store = self._load_store()
        _validate_schedule_for_add(schedule)
        now = _now_ms()

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
                owner_id=owner_id,
                metadata=metadata or {},
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )

        store.jobs.append(job)
        self._save_store()
        self._arm_timer()

        logger.info("Cron: added job '{}' ({})", name, job.id)
        return job

    def remove_job(
        self,
        job_id: str,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        bot_id: str | None = None,
    ) -> bool:
        """Remove a job by ID, optionally verifying tenant, ownership, and bot scope."""
        store = self._load_store()
        before = len(store.jobs)

        if tenant_id or owner_id or bot_id:
            store.jobs = [
                j
                for j in store.jobs
                if j.id != job_id
                or not _job_matches_scope(j, tenant_id=tenant_id, owner_id=owner_id, bot_id=bot_id)
            ]
        else:
            store.jobs = [j for j in store.jobs if j.id != job_id]

        removed = len(store.jobs) < before

        if removed:
            if self._external_store:
                self._external_store.delete_job(job_id)
            self._save_store()
            self._arm_timer()
            logger.info("Cron: removed job {}", job_id)

        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
