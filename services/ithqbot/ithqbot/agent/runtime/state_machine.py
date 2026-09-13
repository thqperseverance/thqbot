from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuntimeStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_TOOL = "WAITING_TOOL"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"


_ALLOWED_TRANSITIONS: dict[RuntimeStatus, set[RuntimeStatus]] = {
    RuntimeStatus.CREATED: {RuntimeStatus.RUNNING, RuntimeStatus.CANCELLED, RuntimeStatus.EXPIRED},
    RuntimeStatus.RUNNING: {
        RuntimeStatus.WAITING_HUMAN,
        RuntimeStatus.WAITING_TOOL,
        RuntimeStatus.PAUSED,
        RuntimeStatus.CANCELLING,
        RuntimeStatus.FAILED,
        RuntimeStatus.COMPLETED,
        RuntimeStatus.EXPIRED,
    },
    RuntimeStatus.WAITING_HUMAN: {
        RuntimeStatus.RUNNING,
        RuntimeStatus.PAUSED,
        RuntimeStatus.CANCELLING,
        RuntimeStatus.CANCELLED,
        RuntimeStatus.EXPIRED,
    },
    RuntimeStatus.WAITING_TOOL: {
        RuntimeStatus.RUNNING,
        RuntimeStatus.CANCELLING,
        RuntimeStatus.FAILED,
        RuntimeStatus.EXPIRED,
    },
    RuntimeStatus.PAUSED: {
        RuntimeStatus.RUNNING,
        RuntimeStatus.CANCELLING,
        RuntimeStatus.CANCELLED,
        RuntimeStatus.EXPIRED,
    },
    RuntimeStatus.CANCELLING: {
        RuntimeStatus.CANCELLED,
        RuntimeStatus.FAILED,
        RuntimeStatus.EXPIRED,
    },
    RuntimeStatus.CANCELLED: set(),
    RuntimeStatus.FAILED: set(),
    RuntimeStatus.COMPLETED: set(),
    RuntimeStatus.EXPIRED: set(),
}


@dataclass(frozen=True)
class RuntimeTransition:
    from_status: RuntimeStatus
    to_status: RuntimeStatus
    allowed: bool


class AgentRuntimeStateMachine:
    @staticmethod
    def normalize(value: str | RuntimeStatus) -> RuntimeStatus:
        if isinstance(value, RuntimeStatus):
            return value
        return RuntimeStatus(str(value).strip().upper())

    @classmethod
    def can_transition(cls, current: str | RuntimeStatus, target: str | RuntimeStatus) -> bool:
        current_status = cls.normalize(current)
        target_status = cls.normalize(target)
        if current_status == target_status:
            return True
        return target_status in _ALLOWED_TRANSITIONS.get(current_status, set())

    @classmethod
    def transition(cls, current: str | RuntimeStatus, target: str | RuntimeStatus) -> RuntimeTransition:
        current_status = cls.normalize(current)
        target_status = cls.normalize(target)
        return RuntimeTransition(
            from_status=current_status,
            to_status=target_status,
            allowed=cls.can_transition(current_status, target_status),
        )
