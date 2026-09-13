from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from threading import RLock
from typing import Any


class CancellationError(Exception):
    def __init__(self, reason: str = "cancelled"):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CancellationSnapshot:
    token_id: str
    parent_token_id: str | None = None
    cancelled: bool = False
    reason: str | None = None
    cancelled_at_ms: int | None = None
    deadline_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "parent_token_id": self.parent_token_id,
            "cancelled": self.cancelled,
            "reason": self.reason,
            "cancelled_at_ms": self.cancelled_at_ms,
            "deadline_ms": self.deadline_ms,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "CancellationSnapshot":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            token_id=str(data.get("token_id") or ""),
            parent_token_id=str(data.get("parent_token_id") or "").strip() or None,
            cancelled=bool(data.get("cancelled")),
            reason=str(data.get("reason") or "").strip() or None,
            cancelled_at_ms=int(data["cancelled_at_ms"]) if data.get("cancelled_at_ms") is not None else None,
            deadline_ms=int(data["deadline_ms"]) if data.get("deadline_ms") is not None else None,
            metadata=dict(data.get("metadata") or {}),
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


class CancellationToken:
    def __init__(
        self,
        *,
        token_id: str | None = None,
        parent: "CancellationToken | None" = None,
        deadline_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.token_id = token_id or f"ctok-{uuid.uuid4()}"
        self._parent = parent
        self._deadline_ms = deadline_ms
        self._metadata = dict(metadata or {})
        self._lock = RLock()
        self._cancelled = False
        self._reason: str | None = None
        self._cancelled_at_ms: int | None = None

    @property
    def parent_token_id(self) -> str | None:
        return self._parent.token_id if self._parent is not None else None

    @property
    def deadline_ms(self) -> int | None:
        return self._deadline_ms

    @property
    def reason(self) -> str | None:
        if self._cancelled:
            return self._reason
        if self._parent is not None and self._parent.is_cancelled():
            return self._parent.reason
        if self._deadline_ms is not None and _now_ms() >= self._deadline_ms:
            return self._reason or "timeout"
        return None

    def is_cancelled(self) -> bool:
        with self._lock:
            if self._cancelled:
                return True
        if self._parent is not None and self._parent.is_cancelled():
            return True
        if self._deadline_ms is not None and _now_ms() >= self._deadline_ms:
            return True
        return False

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancellationError(self.reason or "cancelled")

    def snapshot(self) -> CancellationSnapshot:
        cancelled = self.is_cancelled()
        return CancellationSnapshot(
            token_id=self.token_id,
            parent_token_id=self.parent_token_id,
            cancelled=cancelled,
            reason=self.reason,
            cancelled_at_ms=self._cancelled_at_ms,
            deadline_ms=self._deadline_ms,
            metadata=dict(self._metadata),
        )

    def _cancel(self, reason: str = "cancelled", metadata: dict[str, Any] | None = None) -> bool:
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._reason = reason
            self._cancelled_at_ms = _now_ms()
            if metadata:
                self._metadata.update(metadata)
            return True


class CancellationTokenSource:
    def __init__(
        self,
        *,
        parent: CancellationToken | None = None,
        timeout_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        deadline_ms = _now_ms() + int(timeout_ms) if timeout_ms and timeout_ms > 0 else None
        self._token = CancellationToken(parent=parent, deadline_ms=deadline_ms, metadata=metadata)

    @property
    def token(self) -> CancellationToken:
        return self._token

    def cancel(self, reason: str = "cancelled", metadata: dict[str, Any] | None = None) -> bool:
        return self._token._cancel(reason=reason, metadata=metadata)

    def child(self, *, timeout_ms: int | None = None, metadata: dict[str, Any] | None = None) -> "CancellationTokenSource":
        return CancellationTokenSource(parent=self._token, timeout_ms=timeout_ms, metadata=metadata)

    def snapshot(self) -> CancellationSnapshot:
        return self._token.snapshot()
