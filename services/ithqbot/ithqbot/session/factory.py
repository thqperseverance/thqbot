import os
import time
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from ithqbot.session.store import BaseSessionStore, FileSessionStore
from ithqbot.utils.helpers import sanitize_connection_uri, sanitize_log_message

SessionStoreFactory = Callable[[Path, str], BaseSessionStore]
_SESSION_STORE_FACTORIES: dict[str, SessionStoreFactory] = {}


def register_session_store_backend(scheme: str, factory: SessionStoreFactory) -> None:
    normalized = (scheme or "").lower().strip()
    if normalized:
        _SESSION_STORE_FACTORIES[normalized] = factory


register_session_store_backend("redis", lambda _workspace, uri: __import__("ithqbot.session.redis_store", fromlist=["RedisSessionStore"]).RedisSessionStore(uri))
register_session_store_backend("rediss", lambda _workspace, uri: __import__("ithqbot.session.redis_store", fromlist=["RedisSessionStore"]).RedisSessionStore(uri))
register_session_store_backend("postgresql", lambda _workspace, uri: __import__("ithqbot.session.sql_store", fromlist=["PostgreSQLSessionStore"]).PostgreSQLSessionStore(uri))
register_session_store_backend("postgres", lambda _workspace, uri: __import__("ithqbot.session.sql_store", fromlist=["PostgreSQLSessionStore"]).PostgreSQLSessionStore(uri))
register_session_store_backend("mysql", lambda _workspace, uri: __import__("ithqbot.session.sql_store", fromlist=["MySQLSessionStore"]).MySQLSessionStore(uri))
register_session_store_backend("file", lambda workspace, uri: FileSessionStore(Path(uri[7:]) if uri.startswith("file://") else workspace))


def _probe_session_store_health(store: BaseSessionStore) -> None:
    client = getattr(store, "client", None)
    if client is not None and hasattr(client, "ping"):
        client.ping()
        return
    conn = getattr(store, "_conn", None)
    if conn is not None:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            cursor.close()


def create_session_store(
    workspace: Path,
    config_uri: Optional[str] = None,
    require_external_store: bool = False,
) -> BaseSessionStore:
    """
    Factory to create the appropriate session store based on configuration.
    
    Args:
        workspace: Path to the workspace directory.
        config_uri: Storage connection URI (e.g. redis://, postgresql://). 
                    If None, reads from ithqbot config.
                    
    Returns:
        An instance of a BaseSessionStore implementation.
    """
    if not config_uri:
        try:
            from ithqbot.config.loader import load_config
            config = load_config()
            config_uri = config.get_active_session_store_uri()
        except Exception as e:
            logger.warning(f"Failed to load config for session_store_uri: {sanitize_log_message(e)}")

    uri = config_uri or os.getenv("ITHQBOT_SESSION_STORE")
    
    if not uri:
        raise ValueError("session_store_uri is not configured")

    raw_scheme = uri.split("://", 1)[0].lower()
    scheme = raw_scheme.split("+", 1)[0]
    if require_external_store and scheme == "file":
        raise ValueError("Local file session store is not allowed when require_external_store is enabled")
    factory = _SESSION_STORE_FACTORIES.get(scheme)
    if factory:
        safe_uri = sanitize_connection_uri(uri)
        logger.info("Using {} session store: {}", scheme, safe_uri)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                store = factory(workspace, uri)
                _probe_session_store_health(store)
                if attempt > 1:
                    logger.info("Session store {} recovered on attempt {}", safe_uri, attempt)
                return store
            except ImportError as exc:
                raise RuntimeError("Session store backend is unavailable") from exc
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    logger.warning(
                        "Session store {} init failed on attempt {}/3: {}",
                        safe_uri,
                        attempt,
                        sanitize_log_message(exc),
                    )
                    time.sleep(0.1 * (2 ** (attempt - 1)))
        raise RuntimeError(
            f"Session store initialization failed for {safe_uri}: {sanitize_log_message(last_error)}"
        ) from last_error

    raise ValueError(f"Unsupported session store URI scheme: {scheme}")
