import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from loguru import logger

import redis
from ithqbot.session.store import BaseSessionStore
from ithqbot.session.manager import Session
from ithqbot import context

class RedisSessionStore(BaseSessionStore):
    """
    Redis-based session storage strategy.
    Stores entire session objects as JSON strings.
    """
    
    def __init__(self, uri: str):
        """
        Initialize the Redis connection.
        
        Args:
            uri: Redis connection URI (e.g., redis://localhost:6379/0)
        """
        self.uri = uri
        self.client = redis.Redis.from_url(uri, decode_responses=True)
        self.prefix = "ithqbot:session:"
        self._max_attempts = 3
        self._base_delay_seconds = 0.2

    def _reconnect(self) -> None:
        self.client = redis.Redis.from_url(self.uri, decode_responses=True)

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
                    "Redis session store {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    e,
                )
                time.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error
        
    def _get_key(self, session_key: str) -> str:
        """Generate the Redis key for a given session key, considering tenant isolation."""
        tenant_id = context.tenant_id.get()
        if tenant_id:
            return f"{self.prefix}{tenant_id}:{session_key}"
        return f"{self.prefix}default:{session_key}"

    def get_distributed_lock(
        self,
        key: str,
        *,
        timeout_seconds: float = 180.0,
        blocking_timeout_seconds: float = 5.0,
    ):
        lock_key = f"{self._get_key(key)}:lock"
        return self.client.lock(
            lock_key,
            timeout=timeout_seconds,
            blocking_timeout=blocking_timeout_seconds,
            thread_local=False,
        )

    def load(self, key: str) -> Optional[Session]:
        """Load a session from Redis."""
        redis_key = self._get_key(key)
        try:
            data_str = self._run("load", lambda: self.client.get(redis_key))
            if not data_str:
                return None
                
            data = json.loads(data_str)
            
            return Session(
                key=key,
                messages=data.get("messages", []),
                created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
                updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(),
                metadata=data.get("metadata", {}),
                last_consolidated=data.get("last_consolidated", 0)
            )
        except Exception as e:
            logger.error(f"Failed to load session {key} from Redis: {e}")
            return None

    def save(self, session: Session) -> None:
        """Serialize and persist a session to Redis."""
        redis_key = self._get_key(session.key)
        try:
            data = {
                "key": session.key,
                "messages": session.messages,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            self._run("save", lambda: self.client.set(redis_key, json.dumps(data, ensure_ascii=False)))
        except Exception as e:
            logger.error(f"Failed to save session {session.key} to Redis: {e}")

    def delete(self, key: str) -> None:
        """Delete a specified session from Redis."""
        redis_key = self._get_key(key)
        try:
            self._run("delete", lambda: self.client.delete(redis_key))
        except Exception as e:
            logger.error(f"Failed to delete session {key} from Redis: {e}")

    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        List all sessions from Redis. 
        If tenant_id is in context, lists only sessions for that tenant.
        """
        tenant_id = context.tenant_id.get() or "default"
        pattern = f"{self.prefix}{tenant_id}:*"
        
        sessions = []
        try:
            for redis_key in self._run("scan_iter", lambda: list(self.client.scan_iter(match=pattern))):
                data_str = self._run("get", lambda: self.client.get(redis_key))
                if data_str:
                    try:
                        data = json.loads(data_str)
                        sessions.append({
                            "key": data.get("key"),
                            "created_at": data.get("created_at"),
                            "updated_at": data.get("updated_at"),
                            # No physical path in Redis, returning the redis key instead
                            "path": f"redis://{redis_key}" 
                        })
                    except json.JSONDecodeError:
                        continue
                        
        except Exception as e:
            logger.error(f"Failed to list sessions from Redis: {e}")
            
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
