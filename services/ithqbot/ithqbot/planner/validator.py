from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .models import SkillInfo


def validate_graph(graph: dict[str, Any], skills: list[SkillInfo]) -> bool:
    graph_id = str(graph.get("graph_id") or "").strip()
    if not graph_id:
        raise ValueError("graph_id is required")

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("nodes is required")
    if not isinstance(edges, list):
        raise ValueError("edges is required")

    skill_names = {skill.name for skill in skills}
    skill_by_name = {skill.name: skill for skill in skills}
    node_skill_by_id: dict[str, str] = {}
    node_ids: set[str] = set()
    degree = defaultdict(int)

    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("node must be an object")
        node_id = str(node.get("id") or "").strip()
        skill_name = str(node.get("skill") or "").strip()
        if not node_id:
            raise ValueError("node id is required")
        if node_id in node_ids:
            raise ValueError(f"Duplicate node id: {node_id}")
        node_ids.add(node_id)
        if skill_name not in skill_names:
            raise ValueError(f"Skill not found: {skill_name}")
        node_skill_by_id[node_id] = skill_name

    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("edge must be an object")
        from_node = str(edge.get("from") or "").strip()
        to_node = str(edge.get("to") or "").strip()
        if from_node not in node_ids:
            raise ValueError(f"Unknown edge source: {from_node}")
        if to_node not in node_ids:
            raise ValueError(f"Unknown edge target: {to_node}")
        _validate_edge_constraints(
            from_node,
            to_node,
            skill_by_name[node_skill_by_id[from_node]],
            skill_by_name[node_skill_by_id[to_node]],
        )
        degree[from_node] += 1
        degree[to_node] += 1

    if len(node_ids) > 1:
        isolated = [node_id for node_id in node_ids if degree[node_id] == 0]
        if isolated:
            raise ValueError(f"Isolated nodes are not allowed: {', '.join(sorted(isolated))}")

    if has_cycle(graph):
        raise ValueError("Graph has cycle")

    return True


def _validate_edge_constraints(
    from_node: str,
    to_node: str,
    source_skill: SkillInfo,
    target_skill: SkillInfo,
) -> None:
    source_planner = source_skill.planner or {}
    target_planner = target_skill.planner or {}

    allowed_targets = set(source_planner.get("output_to") or [])
    if allowed_targets and target_skill.name not in allowed_targets:
        raise ValueError(
            f"Planner constraint violation: {source_skill.name} cannot output to {target_skill.name}"
            f" ({from_node} -> {to_node})"
        )

    allowed_sources = set(target_planner.get("input_from") or [])
    if allowed_sources and source_skill.name not in allowed_sources:
        raise ValueError(
            f"Planner constraint violation: {target_skill.name} only accepts input from"
            f" {', '.join(sorted(allowed_sources))} ({from_node} -> {to_node})"
        )

    source_incompatible = set(source_planner.get("incompatible_with") or [])
    target_incompatible = set(target_planner.get("incompatible_with") or [])
    if target_skill.name in source_incompatible or source_skill.name in target_incompatible:
        raise ValueError(
            f"Planner constraint violation: incompatible skills {source_skill.name} and"
            f" {target_skill.name} ({from_node} -> {to_node})"
        )

    produced = set((source_skill.semantic or {}).get("produces") or [])
    consumed = set((target_skill.semantic or {}).get("consumes") or [])
    if produced and consumed and produced.isdisjoint(consumed):
        raise ValueError(
            f"Semantic mismatch: {source_skill.name} produces {sorted(produced)} but"
            f" {target_skill.name} consumes {sorted(consumed)} ({from_node} -> {to_node})"
        )


def has_cycle(graph: dict[str, Any]) -> bool:
    nodes = [str(node["id"]) for node in graph.get("nodes", []) if isinstance(node, dict) and node.get("id")]
    indegree = {node_id: 0 for node_id in nodes}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes}

    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        from_node = str(edge.get("from") or "")
        to_node = str(edge.get("to") or "")
        if from_node not in indegree or to_node not in indegree:
            continue
        adjacency[from_node].append(to_node)
        indegree[to_node] += 1

    queue = deque(node_id for node_id, value in indegree.items() if value == 0)
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        for child in adjacency[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    return visited != len(nodes)
