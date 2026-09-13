"""Build capability declarations and registry entries for built-in skills.

Default behavior:
1. Prefer formal `capability.json` files as the source of truth
2. Fall back to legacy `schema.json` or `tool/tool_def.json` contracts
3. Validate generated registry entries against `capability-registry.schema.json`
4. Write results to `registries/capability-registry/`

Bootstrap behavior:
- Use `--write-capabilities` to generate or refresh `skills/<skill>/capability.json`
  from the legacy skill contracts plus repository-specific defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PACKAGE_ROOT / "skills"
REGISTRY_DIR = PACKAGE_ROOT / "registries" / "capability-registry"
REGISTRY_SCHEMA_PATH = PACKAGE_ROOT / "docs" / "capability-registry.schema.json"
CAPABILITY_SCHEMA_PATH = PACKAGE_ROOT / "docs" / "capability.schema.json"


SKILL_DEFAULTS: dict[str, dict[str, Any]] = {
    "ai_new_day_report": {
        "capability_name": "news.generate_ai_digest",
        "category": "atomic",
        "tags": ["news", "report", "aibase"],
        "routing": {"type": "internal", "service": "ai-news-report", "timeout_ms": 15000},
        "effects": {"type": "external", "resources": ["aibase", "wecom_webhook"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 15000, "availability": "98.0%"},
        "quality": {"score": 0.72, "source": "manual"},
        "status": "disabled",
        "idempotent": False,
        "retryable": True,
    },
    "check_skill": {
        "capability_name": "skill.check_compliance",
        "category": "atomic",
        "tags": ["skill", "compliance", "audit"],
        "routing": {"type": "internal", "service": "check-skill", "timeout_ms": 10000},
        "effects": {"type": "read", "resources": ["skill_workspace"]},
        "cost": {"level": "low"},
        "sla": {"latency_ms": 3000, "availability": "99.5%"},
        "quality": {"score": 0.93, "source": "manual"},
        "idempotent": True,
        "retryable": True,
    },
    "cron": {
        "capability_name": "schedule.manage",
        "category": "atomic",
        "tags": ["schedule", "reminder", "cron"],
        "routing": {"type": "internal", "service": "cron", "timeout_ms": 3000},
        "effects": {"type": "write", "resources": ["scheduler_store"]},
        "cost": {"level": "low"},
        "sla": {"latency_ms": 1000, "availability": "99.9%"},
        "quality": {"score": 0.95, "source": "manual"},
        "idempotent": False,
        "retryable": False,
    },
    "doc_compare": {
        "capability_name": "document.compare",
        "category": "ai",
        "tags": ["document", "comparison", "report"],
        "routing": {"type": "internal", "service": "doc-compare", "timeout_ms": 10000},
        "effects": {"type": "write", "resources": ["object_storage"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 8000, "availability": "99.0%"},
        "quality": {"score": 0.86, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "eval_analyzer": {
        "capability_name": "testing.analyze_failures",
        "category": "ai",
        "tags": ["testing", "analysis", "test_eval"],
        "routing": {"type": "internal", "service": "eval-analyzer", "timeout_ms": 10000},
        "effects": {"type": "external", "resources": ["test_eval_service"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 6000, "availability": "99.0%"},
        "quality": {"score": 0.83, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "file_summary_skill": {
        "capability_name": "document.summarize_file",
        "category": "ai",
        "tags": ["document", "summary", "file"],
        "routing": {"type": "internal", "service": "file-summary", "timeout_ms": 10000},
        "effects": {"type": "write", "resources": ["object_storage"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 8000, "availability": "99.0%"},
        "quality": {"score": 0.82, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "file_to_markdown": {
        "capability_name": "document.to_markdown",
        "category": "atomic",
        "tags": ["document", "markdown", "conversion"],
        "routing": {
            "type": "http",
            "service": "document-convert-service",
            "endpoint": "/convert/markdown",
            "method": "POST",
            "environment": "prod",
            "timeout_ms": 10000,
        },
        "effects": {"type": "write", "resources": ["object_storage"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 5000, "availability": "99.5%"},
        "quality": {"score": 0.84, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "knowledge_retrieval": {
        "capability_name": "knowledge.retrieve",
        "category": "atomic",
        "tags": ["knowledge", "retrieval", "rag"],
        "routing": {"type": "internal", "service": "knowledge-retrieval", "timeout_ms": 3000},
        "effects": {"type": "read", "resources": ["knowledge_index"]},
        "cost": {"level": "low"},
        "sla": {"latency_ms": 1200, "availability": "99.9%"},
        "quality": {"score": 0.88, "source": "manual"},
        "idempotent": True,
        "retryable": True,
    },
    "model_compare": {
        "capability_name": "testing.compare_models",
        "category": "ai",
        "tags": ["testing", "comparison", "models"],
        "routing": {"type": "internal", "service": "model-compare", "timeout_ms": 10000},
        "effects": {"type": "external", "resources": ["test_eval_service"]},
        "cost": {"level": "high"},
        "sla": {"latency_ms": 9000, "availability": "99.0%"},
        "quality": {"score": 0.82, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "official_doc_review": {
        "capability_name": "document.review_official",
        "category": "ai",
        "tags": ["document", "review", "official_document"],
        "routing": {"type": "internal", "service": "official-doc-review", "timeout_ms": 10000},
        "effects": {"type": "write", "resources": ["object_storage"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 7000, "availability": "99.0%"},
        "quality": {"score": 0.87, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "summarize": {
        "capability_name": "content.summarize",
        "category": "ai",
        "tags": ["content", "summary", "web"],
        "routing": {"type": "internal", "service": "summarize-cli", "timeout_ms": 180000},
        "effects": {"type": "external", "resources": ["remote_content"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 15000, "availability": "98.0%"},
        "quality": {"score": 0.80, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "test_planner": {
        "capability_name": "testing.plan_cases",
        "category": "ai",
        "tags": ["testing", "planning", "test_eval"],
        "routing": {"type": "internal", "service": "test-planner", "timeout_ms": 10000},
        "effects": {"type": "external", "resources": ["test_eval_service"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 6000, "availability": "99.0%"},
        "quality": {"score": 0.81, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
    "test_runner": {
        "capability_name": "testing.run",
        "category": "atomic",
        "tags": ["testing", "execution", "datasets"],
        "routing": {"type": "internal", "service": "test-runner", "timeout_ms": 10000},
        "effects": {"type": "external", "resources": ["test_orchestrator_service"]},
        "cost": {"level": "medium"},
        "sla": {"latency_ms": 4000, "availability": "99.5%"},
        "quality": {"score": 0.90, "source": "manual"},
        "idempotent": False,
        "retryable": True,
    },
}


def _capability_path(skill_dir: Path) -> Path:
    return skill_dir / "capability.json"


def _contract_candidates(skill_dir: Path) -> list[Path]:
    return [
        skill_dir / "schema.json",
        skill_dir / "tool" / "tool_def.json",
        skill_dir / "tool_def.json",
    ]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_capability(skill_dir: Path) -> tuple[dict[str, Any], Path] | None:
    path = _capability_path(skill_dir)
    if not path.exists():
        return None
    payload = _load_json(path)
    return (payload, path) if payload else None


def _load_contract(skill_dir: Path) -> tuple[dict[str, Any], Path] | None:
    for candidate in _contract_candidates(skill_dir):
        if not candidate.exists():
            continue
        payload = _load_json(candidate)
        if payload:
            return payload, candidate
    return None


def _normalize_semantic_items(items: Any) -> list[str]:
    normalized: list[str] = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in normalized:
            normalized.append(name)
    return normalized


def _normalize_registry_semantic(
    semantic: Any,
    default_semantic: dict[str, Any] | None,
) -> dict[str, list[str]]:
    if isinstance(semantic, dict):
        produces = _normalize_semantic_items(semantic.get("produces"))
        consumes = _normalize_semantic_items(semantic.get("consumes"))
        if produces and consumes:
            return {"produces": produces, "consumes": consumes}
    if isinstance(default_semantic, dict):
        produces = _normalize_semantic_items(default_semantic.get("produces"))
        consumes = _normalize_semantic_items(default_semantic.get("consumes"))
        if produces and consumes:
            return {"produces": produces, "consumes": consumes}
    raise ValueError("Missing semantic mapping")


def _resolve_input_schema(contract: dict[str, Any]) -> dict[str, Any]:
    schema = contract.get("input_schema")
    if isinstance(schema, dict):
        return deepcopy(schema)
    schema = contract.get("parameters")
    if isinstance(schema, dict):
        return deepcopy(schema)
    return {"type": "object", "properties": {}}


def _resolve_output_schema(contract: dict[str, Any]) -> dict[str, Any]:
    schema = contract.get("output_schema")
    if isinstance(schema, dict):
        return deepcopy(schema)
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "result": {"type": "string"},
        },
    }


def _build_typed_semantic(simple_semantic: dict[str, list[str]]) -> dict[str, list[dict[str, str]]]:
    return {
        "produces": [
            {
                "name": name,
                "schema_ref": "#/output_schema",
                "description": f"Normalized output semantic `{name}`.",
            }
            for name in simple_semantic["produces"]
        ],
        "consumes": [
            {
                "name": name,
                "source": "input",
                "schema_ref": "#/input_schema",
                "description": f"Normalized input semantic `{name}`.",
            }
            for name in simple_semantic["consumes"]
        ],
    }


def _build_checksum(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _build_capability_document(skill_name: str, contract: dict[str, Any]) -> dict[str, Any]:
    defaults = deepcopy(SKILL_DEFAULTS[skill_name])
    capability_name = str(defaults.pop("capability_name"))
    namespace, _action = capability_name.split(".", 1)
    version = str(contract.get("version") or defaults.pop("version", "1.0.0"))
    deprecated = bool(contract.get("deprecated", False))
    simple_semantic = _normalize_registry_semantic(contract.get("semantic"), defaults.pop("semantic", None))

    capability: dict[str, Any] = {
        "name": capability_name,
        "description": str(contract.get("description") or capability_name),
        "version": version,
        "namespace": namespace,
        "category": defaults.pop("category"),
        "tags": defaults.pop("tags"),
        "deprecated": deprecated,
        "input_schema": _resolve_input_schema(contract),
        "output_schema": _resolve_output_schema(contract),
        "semantic": _build_typed_semantic(simple_semantic),
        "routing": defaults.pop("routing"),
        "effects": defaults.pop("effects"),
        "idempotent": bool(defaults.pop("idempotent")),
        "retryable": bool(defaults.pop("retryable")),
    }

    for optional_key in ("dependencies", "auth", "sla", "cost", "quality"):
        value = contract.get(optional_key)
        if value is None:
            value = defaults.pop(optional_key, None)
        if value is not None:
            capability[optional_key] = value

    return capability


def _build_entry(skill_name: str, capability: dict[str, Any], source_path: Path) -> dict[str, Any]:
    defaults = deepcopy(SKILL_DEFAULTS[skill_name])
    deprecated = bool(capability.get("deprecated", False))
    status = str(defaults.get("status", "deprecated" if deprecated else "active"))
    semantic = _normalize_registry_semantic(capability.get("semantic"), defaults.get("semantic"))

    entry_capability: dict[str, Any] = {
        "name": capability["name"],
        "description": capability["description"],
        "version": capability["version"],
        "namespace": capability["namespace"],
        "category": capability.get("category"),
        "tags": capability.get("tags"),
        "deprecated": deprecated,
        "semantic": semantic,
        "routing": capability["routing"],
        "effects": capability["effects"],
        "idempotent": capability["idempotent"],
        "retryable": capability["retryable"],
    }
    for key in ("dependencies", "auth", "sla", "cost", "quality"):
        if key in capability:
            entry_capability[key] = capability[key]

    return {
        "name": capability["name"],
        "plugin": skill_name,
        "version": capability["version"],
        "status": status,
        "capability": entry_capability,
        "source": {
            "schema_file": source_path.relative_to(PACKAGE_ROOT).as_posix(),
            "checksum": _build_checksum(source_path),
        },
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def _validate_documents(schema_path: Path, documents: list[dict[str, Any]], label: str) -> None:
    try:
        import jsonschema
    except Exception as exc:  # pragma: no cover - environment-specific dependency
        raise RuntimeError(f"jsonschema is required to validate {label}") from exc

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for document in documents:
        errors = sorted(validator.iter_errors(document), key=lambda item: list(item.path))
        if errors:
            details = "; ".join(error.message for error in errors)
            raise ValueError(f"{label} validation failed for {document.get('name')}: {details}")


def _iter_supported_skills() -> list[Path]:
    return [
        skill_dir
        for skill_dir in sorted(SKILLS_DIR.iterdir())
        if skill_dir.is_dir() and skill_dir.name in SKILL_DEFAULTS
    ]


def build_capabilities() -> list[tuple[str, dict[str, Any]]]:
    documents: list[tuple[str, dict[str, Any]]] = []
    for skill_dir in _iter_supported_skills():
        loaded = _load_contract(skill_dir)
        if loaded is None:
            continue
        contract, _contract_path = loaded
        documents.append((skill_dir.name, _build_capability_document(skill_dir.name, contract)))
    return documents


def write_capabilities(documents: list[tuple[str, dict[str, Any]]]) -> None:
    for skill_name, document in documents:
        output_path = _capability_path(SKILLS_DIR / skill_name)
        output_path.write_text(
            json.dumps(document, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )


def build_registry() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for skill_dir in _iter_supported_skills():
        skill_name = skill_dir.name
        loaded_capability = _load_capability(skill_dir)
        if loaded_capability is not None:
            capability, source_path = loaded_capability
            entries.append(_build_entry(skill_name, capability, source_path))
            continue

        loaded_contract = _load_contract(skill_dir)
        if loaded_contract is None:
            continue
        contract, contract_path = loaded_contract
        capability = _build_capability_document(skill_name, contract)
        entries.append(_build_entry(skill_name, capability, contract_path))
    return entries


def write_registry(entries: list[dict[str, Any]]) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        output_path = REGISTRY_DIR / f"{entry['name']}.json"
        output_path.write_text(
            json.dumps(entry, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build capability files and registry entries.")
    parser.add_argument(
        "--write-capabilities",
        action="store_true",
        help="Generate or refresh skills/<skill>/capability.json before building the registry.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    capability_docs: list[tuple[str, dict[str, Any]]] = []

    if args.write_capabilities:
        capability_docs = build_capabilities()
        _validate_documents(
            CAPABILITY_SCHEMA_PATH,
            [document for _, document in capability_docs],
            "Capability",
        )
        write_capabilities(capability_docs)

    entries = build_registry()
    _validate_documents(REGISTRY_SCHEMA_PATH, entries, "Registry")
    write_registry(entries)

    if capability_docs:
        print(f"Generated {len(capability_docs)} capability files under {SKILLS_DIR}")
    print(f"Generated {len(entries)} capability registry entries in {REGISTRY_DIR}")


if __name__ == "__main__":
    main()
