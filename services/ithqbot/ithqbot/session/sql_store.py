import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from ithqbot import context
from ithqbot.session.manager import Session
from ithqbot.session.store import BaseSessionStore


def _tenant_id() -> str:
    return context.tenant_id.get() or "default"


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return ""


class PostgreSQLSessionStore(BaseSessionStore):
    def __init__(self, uri: str):
        self.uri = uri
        self._driver_name = ""
        self._conn = None
        self._lock = threading.Lock()
        self._max_attempts = 3
        self._base_delay_seconds = 0.2
        self._table_initialized = False

    def _connect(self):
        import psycopg

        self._driver_name = "psycopg"
        return psycopg.connect(self.uri, autocommit=True)

    def _ensure_conn(self):
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    self._conn = self._connect()
                    if not self._table_initialized:
                        self._init_table_sync()
        return self._conn

    def _reconnect(self) -> None:
        with self._lock:
            try:
                if self._conn:
                    self._conn.close()
            except Exception:
                pass
            self._conn = self._connect()

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
                    "PostgreSQL session store {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    e,
                )
                time.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error

    @contextmanager
    def _cursor(self):
        conn = self._ensure_conn()
        with self._lock:
            cur = conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def _init_table_sync(self) -> None:
        """Initialize tables without using _run to avoid recursion."""
        try:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ithqbot_sessions (
                        tenant_id TEXT NOT NULL,
                        session_key TEXT NOT NULL,
                        data TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (tenant_id, session_key)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ithqbot_sessions_tenant_updated
                    ON ithqbot_sessions(tenant_id, updated_at DESC)
                    """
                )
                self._table_initialized = True
            finally:
                cur.close()
        except Exception as e:
            logger.error("Failed to initialize PostgreSQL session table: {}", e)
            raise

    def load(self, key: str) -> Session | None:
        tenant = _tenant_id()
        try:
            def _op():
                with self._cursor() as cur:
                    cur.execute(
                        "SELECT data FROM ithqbot_sessions WHERE tenant_id = %s AND session_key = %s",
                        (tenant, key),
                    )
                    return cur.fetchone()
            row = self._run("load", _op)
            if not row:
                return None
            data = json.loads(row[0])
            return Session(
                key=key,
                messages=data.get("messages", []),
                created_at=datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else datetime.now(),
                updated_at=datetime.fromisoformat(data["updated_at"])
                if data.get("updated_at")
                else datetime.now(),
                metadata=data.get("metadata", {}),
                last_consolidated=data.get("last_consolidated", 0),
            )
        except Exception as e:
            logger.error("Failed to load session {} from PostgreSQL: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        tenant = _tenant_id()
        payload = json.dumps(
            {
                "key": session.key,
                "messages": session.messages,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated,
            },
            ensure_ascii=False,
        )
        try:
            def _op() -> None:
                with self._cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ithqbot_sessions (tenant_id, session_key, data)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (tenant_id, session_key)
                        DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                        """,
                        (tenant, session.key, payload),
                    )
            self._run("save", _op)
        except Exception as e:
            logger.error("Failed to save session {} to PostgreSQL: {}", session.key, e)

    def delete(self, key: str) -> None:
        tenant = _tenant_id()
        try:
            def _op() -> None:
                with self._cursor() as cur:
                    cur.execute(
                        "DELETE FROM ithqbot_sessions WHERE tenant_id = %s AND session_key = %s",
                        (tenant, key),
                    )
            self._run("delete", _op)
        except Exception as e:
            logger.error("Failed to delete session {} from PostgreSQL: {}", key, e)

    def list_sessions(self) -> list[dict[str, Any]]:
        tenant = _tenant_id()
        items: list[dict[str, Any]] = []
        try:
            def _op():
                with self._cursor() as cur:
                    cur.execute(
                        """
                        SELECT session_key, created_at, updated_at
                        FROM ithqbot_sessions
                        WHERE tenant_id = %s
                        ORDER BY updated_at DESC
                        """,
                        (tenant,),
                    )
                    return cur.fetchall() or []
            rows = self._run("list_sessions", _op)
            for row in rows:
                items.append(
                    {
                        "key": row[0],
                        "created_at": _to_iso(row[1]),
                        "updated_at": _to_iso(row[2]),
                        "path": f"postgresql://ithqbot_sessions/{tenant}/{row[0]}",
                    }
                )
        except Exception as e:
            logger.error("Failed to list sessions from PostgreSQL: {}", e)
        return items


class MySQLSessionStore(BaseSessionStore):
    def __init__(self, uri: str):
        self.uri = uri
        self._conn = self._connect(uri)
        self._max_attempts = 3
        self._base_delay_seconds = 0.2
        self._init_table()

    def _connect(self, uri: str):
        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 3306
        user = unquote(parsed.username) if parsed.username else ""
        password = unquote(parsed.password) if parsed.password else ""
        database = (parsed.path or "/").lstrip("/")
        try:
            import pymysql

            return pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
                autocommit=True,
            )
        except Exception:
            import mysql.connector

            conn = mysql.connector.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
            )
            conn.autocommit = True
            return conn

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
                    "MySQL session store {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    e,
                )
                time.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def _init_table(self) -> None:
        def _op() -> None:
            with self._cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ithqbot_sessions (
                        tenant_id VARCHAR(255) NOT NULL,
                        session_key VARCHAR(255) NOT NULL,
                        data LONGTEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (tenant_id, session_key),
                        KEY idx_ithqbot_sessions_tenant_updated (tenant_id, updated_at)
                    )
                    """
                )
        self._run("init_table", _op)

    def load(self, key: str) -> Session | None:
        tenant = _tenant_id()
        try:
            def _op():
                with self._cursor() as cur:
                    cur.execute(
                        "SELECT data FROM ithqbot_sessions WHERE tenant_id = %s AND session_key = %s",
                        (tenant, key),
                    )
                    return cur.fetchone()
            row = self._run("load", _op)
            if not row:
                return None
            data = json.loads(row[0])
            return Session(
                key=key,
                messages=data.get("messages", []),
                created_at=datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else datetime.now(),
                updated_at=datetime.fromisoformat(data["updated_at"])
                if data.get("updated_at")
                else datetime.now(),
                metadata=data.get("metadata", {}),
                last_consolidated=data.get("last_consolidated", 0),
            )
        except Exception as e:
            logger.error("Failed to load session {} from MySQL: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        tenant = _tenant_id()
        payload = json.dumps(
            {
                "key": session.key,
                "messages": session.messages,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated,
            },
            ensure_ascii=False,
        )
        try:
            def _op() -> None:
                with self._cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ithqbot_sessions (tenant_id, session_key, data)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE data = VALUES(data), updated_at = CURRENT_TIMESTAMP
                        """,
                        (tenant, session.key, payload),
                    )
            self._run("save", _op)
        except Exception as e:
            logger.error("Failed to save session {} to MySQL: {}", session.key, e)

    def delete(self, key: str) -> None:
        tenant = _tenant_id()
        try:
            def _op() -> None:
                with self._cursor() as cur:
                    cur.execute(
                        "DELETE FROM ithqbot_sessions WHERE tenant_id = %s AND session_key = %s",
                        (tenant, key),
                    )
            self._run("delete", _op)
        except Exception as e:
            logger.error("Failed to delete session {} from MySQL: {}", key, e)

    def list_sessions(self) -> list[dict[str, Any]]:
        tenant = _tenant_id()
        items: list[dict[str, Any]] = []
        try:
            def _op():
                with self._cursor() as cur:
                    cur.execute(
                        """
                        SELECT session_key, created_at, updated_at
                        FROM ithqbot_sessions
                        WHERE tenant_id = %s
                        ORDER BY updated_at DESC
                        """,
                        (tenant,),
                    )
                    return cur.fetchall() or []
            rows = self._run("list_sessions", _op)
            for row in rows:
                items.append(
                    {
                        "key": row[0],
                        "created_at": _to_iso(row[1]),
                        "updated_at": _to_iso(row[2]),
                        "path": f"mysql://ithqbot_sessions/{tenant}/{row[0]}",
                    }
                )
        except Exception as e:
            logger.error("Failed to list sessions from MySQL: {}", e)
        return items
