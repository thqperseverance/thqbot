from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Edge:
    from_node: str
    to_node: str
    condition: str | None = None


@dataclass
class Node:
    id: str
    skill: str
    status: str = "pending"
    input: dict[str, Any] = field(default_factory=dict)
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class Graph:
    graph_id: str
    nodes: dict[str, Node]
    edges: list[Edge]


@dataclass
class GraphRun:
    run_id: str
    graph_id: str
    status: str
    state: dict[str, Any] = field(default_factory=dict)
    node_results: dict[str, Any] = field(default_factory=dict)
