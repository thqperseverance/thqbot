from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import Edge, Graph, Node
from .scheduler import validate_graph


def load_graph(path: str) -> Graph:
    graph_path = Path(path)
    with graph_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    return load_graph_data(data)


def load_graph_from_dict(data: dict[str, Any]) -> Graph:
    return load_graph_data(data)


def get_graph_search_dirs(
    workspace: str | Path | None = None,
    builtin_dir: str | Path | None = None,
    builtin_skills_dir: str | Path | None = None,
) -> list[Path]:
    search_dirs: list[Path] = []
    if workspace is not None:
        workspace_path = Path(workspace)
        search_dirs.extend(
            [
                workspace_path / "graphs",
                workspace_path / "skills_graphs",
                workspace_path / "skills",
            ]
        )
    if builtin_dir is not None:
        search_dirs.append(Path(builtin_dir))
    if builtin_skills_dir is not None:
        search_dirs.append(Path(builtin_skills_dir))
    return [path for path in search_dirs if path.exists() and path.is_dir()]


def load_graph_by_id(graph_id: str, search_dirs: list[str | Path]) -> Graph:
    graph_name = graph_id.strip()
    if not graph_name:
        raise FileNotFoundError("graph_id is required")

    candidates: list[Path] = []
    for base_dir in search_dirs:
        root = Path(base_dir)
        candidates.extend(
            [
                root / f"{graph_name}.yaml",
                root / f"{graph_name}.yml",
                root / graph_name / "graph.yaml",
                root / graph_name / "graph.yml",
            ]
        )

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return load_graph(str(candidate))

    searched = ", ".join(str(Path(path)) for path in search_dirs)
    raise FileNotFoundError(f"Graph not found: {graph_id} (searched: {searched})")


def load_graph_data(data: dict[str, Any]) -> Graph:
    nodes = {
        item["id"]: Node(
            id=item["id"],
            skill=item["skill"],
            input=dict(item.get("input") or {}),
            input_mapping=dict(item.get("input_mapping") or {}),
            output_mapping=dict(item.get("output_mapping") or {}),
        )
        for item in data.get("nodes", [])
    }

    edges = [
        Edge(
            from_node=item["from"],
            to_node=item["to"],
            condition=item.get("condition"),
        )
        for item in data.get("edges", [])
    ]

    graph = Graph(
        graph_id=str(data["graph_id"]),
        nodes=nodes,
        edges=edges,
    )
    validate_graph(graph)
    return graph
