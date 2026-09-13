from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from pathlib import Path

# We need to import Session from manager, but manager imports from store
# Let's use TYPE_CHECKING or local import to avoid circular dependency
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ithqbot.session.manager import Session

class BaseSessionStore(ABC):
    """Abstract base class for session storage strategies."""

    def should_cache_sessions(self) -> bool:
        """Return whether SessionManager may keep long-lived in-process copies."""
        return False

    def get_distributed_lock(
        self,
        key: str,
        *,
        timeout_seconds: float = 180.0,
        blocking_timeout_seconds: float = 5.0,
    ):
        """Return a best-effort cross-process lock object for a session key."""
        _ = (key, timeout_seconds, blocking_timeout_seconds)
        return None
    
    @abstractmethod
    def load(self, key: str) -> Optional['Session']:
        """Load a session from the storage medium."""
        pass

    @abstractmethod
    def save(self, session: 'Session') -> None:
        """Serialize and persist a session to the storage medium."""
        pass

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete a specified session."""
        pass

    @abstractmethod
    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions. Returns a list of session info dicts."""
        pass

import json
import shutil
from datetime import datetime
from loguru import logger

from ithqbot.config.paths import get_legacy_sessions_dir
from ithqbot.utils.helpers import ensure_dir, safe_filename

class FileSessionStore(BaseSessionStore):
    """File-based session storage using JSONL format."""

    def should_cache_sessions(self) -> bool:
        return True
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.ithqbot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def load(self, key: str) -> Optional['Session']:
        """Load a session from disk."""
        from ithqbot.session.manager import Session
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: 'Session') -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def delete(self, key: str) -> None:
        """Delete a specified session file."""
        path = self._get_session_path(key)
        if path.exists():
            path.unlink()

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions from disk."""
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
