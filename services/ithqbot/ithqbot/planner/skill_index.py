from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import SkillInfo


def load_skills(skill_loader: Any) -> list[SkillInfo]:
    if skill_loader is None:
        return []

    loaded = getattr(skill_loader, "load_skills", None)
    if callable(loaded):
        raw_skills = loaded()
        if isinstance(raw_skills, list) and all(isinstance(item, SkillInfo) for item in raw_skills):
            return raw_skills

    listed = getattr(skill_loader, "list_skills", None)
    if not callable(listed):
        return []

    result: list[SkillInfo] = []
    for item in listed(filter_unavailable=True):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        result.append(
            SkillInfo(
                name=name,
                description=_get_description(skill_loader, name),
                input_schema=_get_input_schema(skill_loader, name, item),
                output_schema=_get_output_schema(skill_loader, name, item),
                semantic=_get_semantic(skill_loader, name, item),
                capability=_get_capability(skill_loader, name),
                tags=_get_tags(skill_loader, name),
                level=_get_level(skill_loader, name),
                planner=_get_planner_hints(skill_loader, name),
                idempotent=_get_bool_meta(skill_loader, name, "idempotent"),
                retryable=_get_bool_meta(skill_loader, name, "retryable"),
                cost=_get_object_meta(skill_loader, name, "cost"),
                latency=_get_object_meta(skill_loader, name, "latency"),
            )
        )
    return result


def build_skill_text(skills: list[SkillInfo]) -> str:
    texts: list[str] = []
    for skill in skills:
        texts.append(
            "\n".join(
                [
                    f"Skill: {skill.name}",
                    f"Description: {skill.description or skill.name}",
                    f"Input: {json.dumps(skill.input_schema or {}, ensure_ascii=False)}",
                    f"Output: {json.dumps(skill.output_schema or {}, ensure_ascii=False)}",
                    f"Semantic: {json.dumps(skill.semantic or {}, ensure_ascii=False)}",
                    f"Capability: {json.dumps(skill.capability or [], ensure_ascii=False)}",
                    f"Tags: {json.dumps(skill.tags or [], ensure_ascii=False)}",
                    f"Level: {skill.level or 'atomic'}",
                    f"Planner: {json.dumps(skill.planner or {}, ensure_ascii=False)}",
                    f"Idempotent: {json.dumps(skill.idempotent, ensure_ascii=False)}",
                    f"Retryable: {json.dumps(skill.retryable, ensure_ascii=False)}",
                    f"Cost: {json.dumps(skill.cost or {}, ensure_ascii=False)}",
                    f"Latency: {json.dumps(skill.latency or {}, ensure_ascii=False)}",
                ]
            )
        )
    return "\n\n".join(texts)


def _get_description(skill_loader: Any, name: str) -> str:
    getter = getattr(skill_loader, "_get_skill_description", None)
    if callable(getter):
        description = getter(name)
        if isinstance(description, str) and description.strip():
            return description.strip()

    metadata_getter = getattr(skill_loader, "get_skill_metadata", None)
    if callable(metadata_getter):
        metadata = metadata_getter(name) or {}
        description = metadata.get("description")
        if isinstance(description, str) and description.strip():
            return description.strip()

    return name


def _get_output_schema(skill_loader: Any, name: str, item: dict[str, Any]) -> dict[str, Any]:
    contract = _load_contract(skill_loader, name, item)
    schema = contract.get("output_schema")
    return schema if isinstance(schema, dict) else {}


def _get_semantic(skill_loader: Any, name: str, item: dict[str, Any]) -> dict[str, list[str]]:
    contract = _load_contract(skill_loader, name, item)
    semantic = contract.get("semantic")
    if not isinstance(semantic, dict):
        return {}
    return {
        key: _normalize_semantic_list(value)
        for key, value in semantic.items()
        if key in {"produces", "consumes"}
    }


def _get_capability(skill_loader: Any, name: str) -> list[str]:
    meta = _get_skill_meta(skill_loader, name)
    capability = _normalize_string_list(meta.get("capability"))
    if capability:
        return capability
    contract = _load_contract(skill_loader, name, {})
    return _normalize_string_list(contract.get("capability"))


def _get_tags(skill_loader: Any, name: str) -> list[str]:
    meta = _get_skill_meta(skill_loader, name)
    tags = _normalize_string_list(meta.get("tags"))
    if tags:
        return tags
    contract = _load_contract(skill_loader, name, {})
    return _normalize_string_list(contract.get("tags"))


def _get_level(skill_loader: Any, name: str) -> str:
    meta = _get_skill_meta(skill_loader, name)
    level = meta.get("level")
    if isinstance(level, str) and level.strip():
        return level.strip().lower()
    contract = _load_contract(skill_loader, name, {})
    category = contract.get("category")
    if isinstance(category, str) and category.strip():
        return category.strip().lower()
    return "atomic"


def _get_planner_hints(skill_loader: Any, name: str) -> dict[str, list[str]]:
    meta = _get_skill_meta(skill_loader, name)
    planner = meta.get("planner")
    if not isinstance(planner, dict):
        contract = _load_contract(skill_loader, name, {})
        planner = contract.get("planner")
        if not isinstance(planner, dict):
            return {}
    allowed_keys = {"input_from", "output_to", "incompatible_with", "preferred_after"}
    return {key: _normalize_string_list(value) for key, value in planner.items() if key in allowed_keys}


def _get_bool_meta(skill_loader: Any, name: str, key: str) -> bool | None:
    meta = _get_skill_meta(skill_loader, name)
    value = meta.get(key)
    if isinstance(value, bool):
        return value
    contract = _load_contract(skill_loader, name, {})
    value = contract.get(key)
    return value if isinstance(value, bool) else None


def _get_object_meta(skill_loader: Any, name: str, key: str) -> dict[str, Any]:
    meta = _get_skill_meta(skill_loader, name)
    value = meta.get(key)
    if isinstance(value, dict):
        return value
    contract = _load_contract(skill_loader, name, {})
    value = contract.get(key)
    if isinstance(value, dict):
        return value
    if key == "latency":
        sla = contract.get("sla")
        if isinstance(sla, dict):
            normalized: dict[str, Any] = {}
            if isinstance(sla.get("latency_ms"), int):
                normalized["expected_ms"] = sla["latency_ms"]
            if isinstance(sla.get("availability"), str) and sla["availability"].strip():
                normalized["availability"] = sla["availability"].strip()
            return normalized
    return {}


def _get_input_schema(skill_loader: Any, name: str, item: dict[str, Any]) -> dict[str, Any]:
    contract = _load_contract(skill_loader, name, item)
    schema = contract.get("input_schema")
    if isinstance(schema, dict):
        return schema
    parameters = contract.get("parameters")
    if isinstance(parameters, dict):
        return parameters
    return {}


def _load_contract(skill_loader: Any, name: str, item: dict[str, Any]) -> dict[str, Any]:
    tool_def_loader = getattr(skill_loader, "_load_tool_def", None)
    if callable(tool_def_loader):
        tool_def = tool_def_loader(name) or {}
        if isinstance(tool_def, dict):
            return tool_def

    skill_path = item.get("path")
    if isinstance(skill_path, str) and skill_path.strip():
        skill_dir = Path(skill_path).resolve().parent
        for candidate in [
            skill_dir / "capability.json",
            skill_dir / "schema.json",
            skill_dir / "tool" / "tool_def.json",
            skill_dir / "tool_def.json",
        ]:
            if not candidate.exists():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(raw, dict):
                return raw
    return {}


def _get_skill_meta(skill_loader: Any, name: str) -> dict[str, Any]:
    metadata_getter = getattr(skill_loader, "get_skill_metadata", None)
    if callable(metadata_getter):
        metadata = metadata_getter(name) or {}
        if not isinstance(metadata, dict):
            return {}
        raw_meta = metadata.get("metadata", metadata)
        parser = getattr(skill_loader, "_parse_ithqbot_metadata", None)
        if callable(parser):
            parsed = parser(raw_meta)
            return parsed if isinstance(parsed, dict) else {}
        if isinstance(raw_meta, dict):
            return raw_meta.get("ithqbot", raw_meta.get("openclaw", raw_meta))
    return {}


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _normalize_semantic_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in normalized:
            normalized.append(name)
    return normalized
