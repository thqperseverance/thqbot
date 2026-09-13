from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ithqbot.providers.base import ToolCallRequest


@dataclass(frozen=True)
class ToolCallNode:
    id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    condition: str | None = None
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "depends_on": list(self.depends_on),
            "condition": self.condition,
            "sequence": self.sequence,
        }


@dataclass(frozen=True)
class ToolCallPlan:
    schema_version: str = "v1"
    plan_version: str = "1.0"
    nodes: tuple[ToolCallNode, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.nodes

    @classmethod
    def from_tool_calls(cls, tool_calls: list[ToolCallRequest], parallel: bool = False) -> "ToolCallPlan":
        nodes: list[ToolCallNode] = []
        previous_node_id: str | None = None
        for index, tool_call in enumerate(tool_calls):
            node_id = str(tool_call.id or f"call_{index + 1}")
            nodes.append(
                ToolCallNode(
                    id=node_id,
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments or {}),
                    depends_on=((previous_node_id,) if previous_node_id and not parallel else ()),
                    sequence=index,
                )
            )
            previous_node_id = node_id
        return cls(nodes=tuple(nodes))

    @classmethod
    def single(
        cls,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        tool_call_id: str,
    ) -> "ToolCallPlan":
        return cls(
            nodes=(
                ToolCallNode(
                    id=tool_call_id,
                    tool_name=tool_name,
                    arguments=dict(arguments or {}),
                ),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_version": self.plan_version,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    def signature(self) -> tuple[str, ...]:
        result: list[str] = []
        for node in self.nodes:
            try:
                normalized_args = json.dumps(
                    node.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            except Exception:
                normalized_args = str(node.arguments)
            result.append(f"{node.tool_name}:{normalized_args}")
        return tuple(result)

    def to_record(self, inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_version": self.plan_version,
            "plan": self.to_dict(),
            "signature": self.signature(),
            "inputs": dict(inputs),
            "outputs": dict(outputs),
        }
