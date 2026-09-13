from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


InteractionType = Literal["text_input", "confirm", "approval", "form", "otp", "file_upload"]


@dataclass(frozen=True)
class HumanInteractionRequest:
    interaction_id: str
    session_id: str
    type: InteractionType
    prompt: str
    schema: dict[str, Any] | None = None
    timeout: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "interaction_id": self.interaction_id,
            "session_id": self.session_id,
            "type": self.type,
            "prompt": self.prompt,
            "schema": dict(self.schema) if isinstance(self.schema, dict) else None,
            "timeout": int(self.timeout),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "HumanInteractionRequest | None":
        if not isinstance(payload, dict):
            return None
        interaction_id = str(payload.get("interaction_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        prompt = str(payload.get("prompt") or payload.get("title") or "").strip()
        if not interaction_id or not session_id:
            return None
        interaction_type = str(payload.get("type") or "form").strip().lower() or "form"
        if interaction_type not in {"text_input", "confirm", "approval", "form", "otp", "file_upload"}:
            interaction_type = "form"
        return cls(
            interaction_id=interaction_id,
            session_id=session_id,
            type=interaction_type,  # type: ignore[arg-type]
            prompt=prompt,
            schema=dict(payload.get("schema") or {}) if isinstance(payload.get("schema"), dict) else None,
            timeout=max(0, int(payload.get("timeout") or 0)),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class HumanInteractionResponse:
    interaction_id: str
    data: Any
    session_id: str | None = None
    submitted_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "interaction_id": self.interaction_id,
            "data": self.data,
            "session_id": self.session_id,
            "submitted_at_ms": self.submitted_at_ms,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "HumanInteractionResponse | None":
        if not isinstance(payload, dict):
            return None
        interaction_id = str(payload.get("interaction_id") or "").strip()
        if not interaction_id:
            return None
        data = payload.get("data")
        if data is None:
            values = payload.get("values")
            data = values if values is not None else payload
        return cls(
            interaction_id=interaction_id,
            data=data,
            session_id=str(payload.get("session_id") or "").strip() or None,
            submitted_at_ms=int(payload["submitted_at_ms"]) if payload.get("submitted_at_ms") is not None else None,
            metadata=dict(payload.get("metadata") or {}),
        )
