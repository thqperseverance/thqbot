from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    semantic: dict[str, list[str]] = field(default_factory=dict)
    capability: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    level: str = "atomic"
    planner: dict[str, list[str]] = field(default_factory=dict)
    idempotent: bool | None = None
    retryable: bool | None = None
    cost: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlannerNode:
    id: str
    skill: str
    input: dict[str, Any] = field(default_factory=dict)
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)


@dataclass
class PlannerEdge:
    from_node: str
    to_node: str
    condition: str | None = None


@dataclass
class PlannerGraph:
    graph_id: str
    nodes: list[PlannerNode]
    edges: list[PlannerEdge]

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "nodes": [
                {
                    "id": node.id,
                    "skill": node.skill,
                    **({"input": node.input} if node.input else {}),
                    **({"input_mapping": node.input_mapping} if node.input_mapping else {}),
                    **({"output_mapping": node.output_mapping} if node.output_mapping else {}),
                }
                for node in self.nodes
            ],
            "edges": [
                {
                    "from": edge.from_node,
                    "to": edge.to_node,
                    **({"condition": edge.condition} if edge.condition else {}),
                }
                for edge in self.edges
            ],
        }
