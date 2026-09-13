from __future__ import annotations

from collections import deque

from .condition import eval_condition
from .exceptions import GraphValidationError
from .models import Graph, GraphRun, Node


TERMINAL_NODE_STATUSES = {"done", "failed"}


def get_parents(graph: Graph, node_id: str) -> list[str]:
    return [e.from_node for e in graph.edges if e.to_node == node_id]


def get_incoming_edges(graph: Graph, node_id: str) -> list:
    return [edge for edge in graph.edges if edge.to_node == node_id]


def validate_graph(graph: Graph) -> None:
    if not graph.graph_id:
        raise GraphValidationError("graph_id is required")
    if not graph.nodes:
        raise GraphValidationError("graph must contain at least one node")

    indegree = {node_id: 0 for node_id in graph.nodes}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}

    for edge in graph.edges:
        if edge.from_node not in graph.nodes:
            raise GraphValidationError(f"Unknown from_node: {edge.from_node}")
        if edge.to_node not in graph.nodes:
            raise GraphValidationError(f"Unknown to_node: {edge.to_node}")
        adjacency[edge.from_node].append(edge.to_node)
        indegree[edge.to_node] += 1

    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in adjacency[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if visited != len(graph.nodes):
        raise GraphValidationError("Graph must be a DAG")


def get_ready_nodes(graph: Graph, run: GraphRun) -> list[Node]:
    ready: list[Node] = []

    for node_id, node in graph.nodes.items():
        if node.status != "pending":
            continue

        incoming_edges = get_incoming_edges(graph, node_id)
        if not incoming_edges:
            ready.append(node)
            continue

        parents = get_parents(graph, node_id)
        if any(graph.nodes[parent_id].status == "failed" for parent_id in parents):
            continue
        if not all(graph.nodes[parent_id].status in TERMINAL_NODE_STATUSES for parent_id in parents):
            continue

        active_edges = [edge for edge in incoming_edges if eval_condition(edge.condition, run.state)]
        if active_edges and all(graph.nodes[edge.from_node].status == "done" for edge in active_edges):
            ready.append(node)

    return ready


def get_skippable_nodes(graph: Graph, run: GraphRun) -> list[Node]:
    skipped: list[Node] = []

    for node_id, node in graph.nodes.items():
        if node.status != "pending":
            continue

        incoming_edges = get_incoming_edges(graph, node_id)
        if not incoming_edges:
            continue

        parents = get_parents(graph, node_id)
        if not all(graph.nodes[parent_id].status in TERMINAL_NODE_STATUSES for parent_id in parents):
            continue

        active_edges = [edge for edge in incoming_edges if eval_condition(edge.condition, run.state)]
        if not active_edges:
            skipped.append(node)

    return skipped
