"""Session management for conversation history."""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _find_legal_start(messages: list[dict[str, Any]]) -> int:
        """Find first index where every tool result has a matching assistant tool_call."""
        declared: set[str] = set()
        start = 0
        for i, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    start = i + 1
                    declared.clear()
                    for prev in messages[start:i + 1]:
                        if prev.get("role") == "assistant":
                            for tc in prev.get("tool_calls") or []:
                                if isinstance(tc, dict) and tc.get("id"):
                                    declared.add(str(tc["id"]))
        return start

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a legal tool-call boundary."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid starting mid-turn when possible.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                sliced = sliced[i:]
                break

        # Some providers reject orphan tool results if the matching assistant
        # tool_calls message fell outside the fixed-size history window.
        start = self._find_legal_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {"role": message["role"]}
            has_tool_calls = bool(message.get("tool_calls"))
            if "content" in message and message.get("content") is not None:
                entry["content"] = message["content"]
            elif not has_tool_calls:
                entry["content"] = ""
            for key in ("tool_calls", "tool_call_id", "name", "metadata", "media"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


from ithqbot.session.store import BaseSessionStore
from pathlib import Path
from typing import Optional, Union

class SessionManager:
    """
    Manages conversation sessions using a configurable storage strategy.
    """

    def __init__(self, workspace: Union[Path, str, None] = None, store: Optional['BaseSessionStore'] = None):
        """
        Initialize the SessionManager.
        
        Args:
            workspace: The workspace path (used to create default FileSessionStore if store is not provided).
            store: The storage strategy to use (e.g. FileSessionStore, RedisSessionStore).
                   If not provided, it will be created using the factory based on config.
        """
        if store is not None:
            self.store = store
        else:
            from ithqbot.session.factory import create_session_store
            from ithqbot.config.paths import get_workspace_path
            
            workspace_path = Path(workspace) if workspace else get_workspace_path()
            self.store = create_session_store(workspace_path)
            
        self._cache: dict[str, Session] = {}
        self._cache_enabled = bool(self.store.should_cache_sessions())

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if self._cache_enabled and key in self._cache:
            return self._cache[key]

        session = self.store.load(key)
        if session is None:
            session = Session(key=key)

        if self._cache_enabled:
            self._cache[key] = session
        return session

    def save(self, session: Session) -> None:
        """Save a session to disk/storage."""
        self.store.save(session)
        if self._cache_enabled:
            self._cache[session.key] = session
        else:
            self._cache.pop(session.key, None)

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        return self.store.list_sessions()

    @asynccontextmanager
    async def session_lock(
        self,
        key: str,
        *,
        timeout_seconds: float = 180.0,
        blocking_timeout_seconds: float = 5.0,
    ):
        """Serialize the same session across instances when the store supports it."""
        lock = self.store.get_distributed_lock(
            key,
            timeout_seconds=timeout_seconds,
            blocking_timeout_seconds=blocking_timeout_seconds,
        )
        if lock is None:
            yield
            return

        acquired = await asyncio.to_thread(lock.acquire)
        if not acquired:
            raise TimeoutError(f"failed to acquire distributed session lock for {key}")

        try:
            yield
        finally:
            with suppress(Exception):
                await asyncio.to_thread(lock.release)
