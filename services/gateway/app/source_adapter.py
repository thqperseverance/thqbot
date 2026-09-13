from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
import httpx

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .s3_compat import S3CompatClient

CONTRACT_VERSION = "v1.2"
logger = logging.getLogger(__name__)

_kafka_producer: AIOKafkaProducer | None = None
_kafka_outbound_consumer: AIOKafkaConsumer | None = None
_kafka_outbound_task: asyncio.Task[None] | None = None
_minio_client: S3CompatClient | None = None
_outbound_consumer_restart_count = 0
_outbound_consumer_last_error = ""
_outbound_consumer_last_started_at_ms = 0
_outbound_consumer_last_event_at_ms = 0


def _ithqbot_repo_root() -> Path:
    configured = os.getenv("ICATMSG_ITHQBOT_REPO", "").strip()
    default_root = (Path(__file__).resolve().parents[3] / "services" / "ithqbot").resolve()
    if configured:
        configured_path = Path(configured).expanduser().resolve()
        if configured_path.exists():
            return configured_path
    return default_root


def _env_first(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return default


def _env_bool(*keys: str, default: bool = False) -> bool:
    value = _env_first(*keys, default="")
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


DEFAULT_TENANT_ID = (
    _env_first("APP_AGENT_TENANT_ID", "ICATMSG_TENANT_ID", "TENANT_ID", default="thqbot") or "thqbot"
)

def _parse_host_port(raw: str, *, default_port: int) -> tuple[str, int]:
    value = raw.strip()
    if not value:
        return "", default_port
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = parsed.hostname or value.split(":", 1)[0].strip("[]")
    port = parsed.port or default_port
    return host, port


def _check_tcp_endpoint(raw: str, *, default_port: int) -> dict[str, Any]:
    host, port = _parse_host_port(raw, default_port=default_port)
    if not host:
        return {
            "endpoint": raw,
            "host": "",
            "port": default_port,
            "reachable": False,
            "reason": "missing host",
        }
    try:
        with socket.create_connection((host, port), timeout=1.0):
            pass
    except Exception as exc:
        return {
            "endpoint": raw,
            "host": host,
            "port": port,
            "reachable": False,
            "reason": str(exc),
        }
    return {
        "endpoint": raw,
        "host": host,
        "port": port,
        "reachable": True,
        "reason": "ok",
    }


def _kafka_bootstrap_servers() -> list[str]:
    raw_servers = _env_first(
        "APP_KAFKA_BOOTSTRAP_SERVERS",
        "ICATMSG_KAFKA_BOOTSTRAP_SERVERS",
        "KAFKA_BOOTSTRAP_SERVERS",
        "KAFKA_SERVERS",
        default="",
    )
    return [item.strip() for item in raw_servers.split(",") if item.strip()]


def _kafka_topics() -> tuple[str, str]:
    inbound = _env_first(
        "APP_KAFKA_INBOUND_TOPIC", "ICATMSG_INBOUND_TOPIC", "KAFKA_INBOUND_TOPIC", default="icatmsg_inbound"
    )
    outbound = _env_first(
        "APP_KAFKA_OUTBOUND_TOPIC",
        "ICATMSG_OUTBOUND_TOPIC",
        "KAFKA_OUTBOUND_TOPIC",
        default="icatmsg_outbound",
    )
    return inbound, outbound


def _kafka_outbound_group_id() -> str:
    return _env_first(
        "APP_KAFKA_OUTBOUND_GROUP",
        "APP_KAFKA_OUTBOUND_GROUP_ID",
        "ICATMSG_KAFKA_OUTBOUND_GROUP_ID",
        "ICATMSG_CORE_GATEWAY_KAFKA_GROUP_ID",
        default="thqbot-gateway",
    )


def _kafka_outbound_enabled() -> bool:
    return _env_bool("APP_KAFKA_ENABLED", "ICATMSG_KAFKA_OUTBOUND_ENABLED", default=True)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _inbound_partition_key(envelope: Any) -> bytes | None:
    """Build a stable Kafka key so related messages land in the same partition."""
    if not isinstance(envelope, dict):
        return None
    body = envelope.get("body") if isinstance(envelope.get("body"), dict) else {}
    metadata = envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
    parts = [
        str(body.get("tenant_id") or metadata.get("tenant_id") or DEFAULT_TENANT_ID).strip(),
        str(body.get("bot_id") or metadata.get("bot_id") or "").strip(),
        str(body.get("account_id") or metadata.get("account_id") or "").strip(),
        str(body.get("chat_id") or metadata.get("chat_id") or "").strip(),
    ]
    key = "|".join(item for item in parts if item)
    if not key:
        return None
    return key.encode("utf-8")


def _build_kafka_auth_options() -> dict[str, Any]:
    protocol = _env_first(
        "ICATMSG_KAFKA_SECURITY_PROTOCOL",
        "KAFKA_SECURITY_PROTOCOL",
        default="",
    )
    if not protocol:
        return {}
    options: dict[str, Any] = {"security_protocol": protocol}
    if protocol.startswith("SASL"):
        username = _env_first(
            "ICATMSG_KAFKA_USERNAME",
            "ICATMSG_KAFKA_USER",
            "KAFKA_USERNAME",
            "KAFKA_USER",
            default="",
        )
        password = _env_first(
            "ICATMSG_KAFKA_PASSWORD",
            "ICATMSG_KAFKA_PASS",
            "KAFKA_PASSWORD",
            "KAFKA_PASS",
            default="",
        )
        mechanism = _env_first(
            "ICATMSG_KAFKA_SASL_MECHANISM",
            "KAFKA_SASL_MECHANISM",
            default="PLAIN",
        ) or "PLAIN"
        if username:
            options["sasl_plain_username"] = username
        if password:
            options["sasl_plain_password"] = password
        options["sasl_mechanism"] = mechanism
    return options


def _minio_endpoint() -> str:
    return _env_first("APP_MINIO_ENDPOINT", "ICATMSG_MINIO_ENDPOINT", "MINIO_ENDPOINT", default="")


def _minio_bucket() -> str:
    return _env_first("APP_MINIO_BUCKET", "ICATMSG_MINIO_BUCKET", "MINIO_BUCKET", default="")


def _minio_secure() -> bool:
    return _env_bool("APP_MINIO_SECURE", "ICATMSG_MINIO_SECURE", "MINIO_SECURE", default=False)


def _minio_credentials() -> tuple[str, str]:
    access_key = _env_first(
        "APP_MINIO_ACCESS_KEY", "ICATMSG_MINIO_ACCESS_KEY", "MINIO_ACCESS_KEY", default=""
    )
    secret_key = _env_first(
        "APP_MINIO_SECRET_KEY", "ICATMSG_MINIO_SECRET_KEY", "MINIO_SECRET_KEY", default=""
    )
    return access_key, secret_key


def ithqbot_repo_status() -> dict[str, Any]:
    root = _ithqbot_repo_root()
    return {
        "configured_path": str(root),
        "exists": root.exists(),
        "config_files": len(_iter_ithqbot_config_paths(root)),
    }


def kafka_status() -> dict[str, Any]:
    servers = _kafka_bootstrap_servers()
    inbound_topic, outbound_topic = _kafka_topics()
    if not servers:
        return {
            "configured": False,
            "available": False,
            "bootstrap_servers": [],
            "reachable_servers": 0,
            "reason": "not configured",
            "inbound_topic": inbound_topic,
            "outbound_topic": outbound_topic,
        }
    results = [_check_tcp_endpoint(item, default_port=9092) for item in servers]
    reachable_servers = len([item for item in results if item["reachable"]])
    return {
        "configured": True,
        "available": reachable_servers > 0,
        "bootstrap_servers": servers,
        "reachable_servers": reachable_servers,
        "servers": results,
        "reason": "ok" if reachable_servers > 0 else "all bootstrap servers unreachable",
        "inbound_topic": inbound_topic,
        "outbound_topic": outbound_topic,
    }


def outbound_consumer_status() -> dict[str, Any]:
    task = _kafka_outbound_task
    enabled = _kafka_outbound_enabled()
    task_running = bool(task is not None and not task.done())
    connected = _kafka_outbound_consumer is not None
    task_state = "stopped"
    if task is not None:
        if task.cancelled():
            task_state = "cancelled"
        elif task.done():
            task_state = "failed" if task.exception() else "stopped"
        else:
            task_state = "running"
    reason = "ok" if enabled and task_running and connected else "outbound consumer unavailable"
    if not enabled:
        reason = "disabled by config"
    elif task is None:
        reason = "not started"
    elif task.done() and task.cancelled():
        reason = "cancelled"
    elif task.done() and _outbound_consumer_last_error:
        reason = _outbound_consumer_last_error
    elif task_running and not connected:
        reason = "connecting"
    return {
        "enabled": enabled,
        "available": enabled and task_running and connected,
        "task_running": task_running,
        "task_state": task_state,
        "consumer_connected": connected,
        "group_id": _kafka_outbound_group_id(),
        "topic": _kafka_topics()[1],
        "restart_count": _outbound_consumer_restart_count,
        "last_started_at_ms": _outbound_consumer_last_started_at_ms,
        "last_event_at_ms": _outbound_consumer_last_event_at_ms,
        "last_error": _outbound_consumer_last_error,
        "reason": reason,
    }


def minio_status() -> dict[str, Any]:
    endpoint = _minio_endpoint()
    bucket = _minio_bucket()
    secure = _minio_secure()
    access_key, secret_key = _minio_credentials()
    if not endpoint:
        return {
            "configured": False,
            "available": False,
            "endpoint": "",
            "bucket": bucket,
            "secure": secure,
            "reason": "not configured",
        }
    network = _check_tcp_endpoint(endpoint, default_port=443 if secure else 9000)
    configured = bool(bucket and access_key and secret_key)
    return {
        "configured": configured,
        "available": configured and bool(network["reachable"]),
        "endpoint": endpoint,
        "bucket": bucket,
        "secure": secure,
        "network": network,
        "reason": "ok" if configured and network["reachable"] else ("bucket or credentials are not configured" if not configured else network["reason"]),
    }


def _ensure_ithqbot_on_path() -> Path | None:
    root = _ithqbot_repo_root()
    if not root.exists():
        return None
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def _load_observability_provider() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    configure_observability: Callable[..., Any]
    get_trace_summary: Callable[..., Any]
    get_trace_task: Callable[..., Any]
    get_trace_task_events: Callable[..., Any]
    get_trace_tasks: Callable[..., Any]
    get_trace_tree: Callable[..., Any]
    get_trace_graph: Callable[..., Any]

    ithqbot_root = _ensure_ithqbot_on_path()
    if ithqbot_root is not None:
        try:
            from ithqbot.observability import (
                configure_observability,
                get_trace_graph,
                get_trace_summary,
                get_trace_task,
                get_trace_task_events,
                get_trace_tasks,
                get_trace_tree,
            )
            return (
                {
                    "configure_observability": configure_observability,
                    "get_trace_tasks": get_trace_tasks,
                    "get_trace_summary": get_trace_summary,
                    "get_trace_task": get_trace_task,
                    "get_trace_task_events": get_trace_task_events,
                    "get_trace_tree": get_trace_tree,
                    "get_trace_graph": get_trace_graph,
                },
                {
                    "available": True,
                    "reason": "ok",
                    "provider": "ithqbot",
                },
            )
        except Exception as exc:
            ithqbot_error = exc
    else:
        ithqbot_error = None

    if ithqbot_error is not None:
        return (
            None,
            {
                "available": False,
                "reason": f"ithqbot import failed: {ithqbot_error}",
                "provider": "ithqbot",
            },
        )
    return (
        None,
        {
            "available": False,
            "reason": "ithqbot repo not found",
        },
    )


def load_observability_api() -> dict[str, Any] | None:
    api, _status = _load_observability_provider()
    if api is None:
        return None

    redis_uri = os.getenv("ICATMSG_REDIS_URI") or os.getenv("REDIS_URL")
    configure_observability = api.get("configure_observability")
    if callable(configure_observability):
        try:
            configure_observability(redis_uri or None)
        except Exception:
            pass
    api.pop("configure_observability", None)
    return api


def observability_status() -> dict[str, Any]:
    _api, status = _load_observability_provider()
    return status


def _iter_ithqbot_config_paths(root: Path | None = None) -> list[Path]:
    base = root or _ithqbot_repo_root()
    if not base.exists():
        return []
    candidates = [path for path in base.glob("config*.json") if path.is_file()]
    return sorted(
        candidates,
        key=lambda path: (
            0 if path.name == "config.json" else 1 if path.name.startswith("config-") else 2,
            path.name,
        ),
    )


def _load_ithqbot_profile_from_config(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    bot = raw.get("bot") if isinstance(raw.get("bot"), dict) else {}
    channels = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    icatmsg = channels.get("icatmsg") if isinstance(channels.get("icatmsg"), dict) else {}
    bot_id = str(bot.get("id") or icatmsg.get("bot_id") or "").strip()
    if not bot_id:
        return None
    enabled = bool(icatmsg.get("enabled")) if icatmsg else False
    return {
        "bot_id": bot_id,
        "title": str(bot.get("name") or bot_id),
        "description": str(bot.get("description") or ""),
        "status": "online" if enabled else "offline",
        "channel": "icatmsg",
    }


def load_bot_profiles() -> list[dict[str, Any]] | None:
    root = _ithqbot_repo_root()
    config_paths = _iter_ithqbot_config_paths(root)
    if not config_paths:
        return None
    profiles: list[dict[str, Any]] = []
    seen_bot_ids: set[str] = set()
    for path in config_paths:
        profile = _load_ithqbot_profile_from_config(path)
        if not profile:
            continue
        bot_id = str(profile.get("bot_id") or "").strip()
        if not bot_id or bot_id in seen_bot_ids:
            continue
        seen_bot_ids.add(bot_id)
        profiles.append(profile)
    return profiles


def bot_profiles_status() -> dict[str, Any]:
    profiles = load_bot_profiles()
    return {
        "available": bool(profiles),
        "count": len(profiles or []),
    }


def normalize_storage_backend(value: Any, default: str = "minio") -> str:
    backend = str(value or "").strip().lower()
    if backend in {"minio", "s3"}:
        return backend
    return default


def parse_storage_uri(path: str) -> tuple[str | None, str | None, str]:
    raw = str(path or "").strip()
    for backend in ("minio", "s3"):
        prefix = f"{backend}://"
        if raw.startswith(prefix):
            bucket, _, rel_path = raw.removeprefix(prefix).partition("/")
            return backend, (bucket or None), rel_path.lstrip("/\\")
    return None, None, raw.lstrip("/\\")


def build_storage_uri(backend: Any, bucket: Any, rel_path: Any) -> str | None:
    backend_text = normalize_storage_backend(backend)
    bucket_text = str(bucket or "").strip()
    rel_path_text = str(rel_path or "").strip().lstrip("/\\")
    if not bucket_text or not rel_path_text:
        return None
    return f"{backend_text}://{bucket_text}/{rel_path_text}"


def split_storage_uri(path: str) -> tuple[str | None, str | None, str]:
    return parse_storage_uri(path)


def split_minio_uri(path: str) -> tuple[str | None, str]:
    _backend, bucket, rel_path = parse_storage_uri(path)
    return bucket, rel_path


def _normalize_storage_payload(storage: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(storage)
    storage_uri = str(normalized.get("storage_uri") or normalized.get("uri") or "").strip()
    path_value = str(normalized.get("path") or "").strip()

    if path_value:
        parsed_backend, parsed_bucket, parsed_path = parse_storage_uri(path_value)
        if parsed_backend and parsed_path:
            storage_uri = storage_uri or path_value
            normalized["path"] = parsed_path
            if parsed_bucket:
                normalized.setdefault("bucket", parsed_bucket)
            normalized["backend"] = normalize_storage_backend(normalized.get("backend") or parsed_backend)

    if storage_uri:
        parsed_backend, parsed_bucket, parsed_path = parse_storage_uri(storage_uri)
        if parsed_backend and parsed_path:
            normalized["path"] = parsed_path
            if parsed_bucket:
                normalized.setdefault("bucket", parsed_bucket)
            normalized["backend"] = normalize_storage_backend(normalized.get("backend") or parsed_backend)

    bucket = str(normalized.get("bucket") or "").strip()
    rel_path = str(normalized.get("path") or "").strip().lstrip("/\\")
    if rel_path:
        normalized["path"] = rel_path
    generated_uri = build_storage_uri(normalized.get("backend"), bucket, rel_path)
    if generated_uri:
        normalized["storage_uri"] = generated_uri
        normalized["backend"] = normalize_storage_backend(normalized.get("backend"))
    elif storage_uri:
        normalized["storage_uri"] = storage_uri
    return normalized


def _build_storage_compatibility_fields(
    storage: dict[str, Any] | None,
    *,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(storage, dict):
        return {}
    payload: dict[str, Any] = {}
    storage_uri = str(storage.get("storage_uri") or "").strip()
    backend = normalize_storage_backend(storage.get("backend")) if storage.get("backend") else ""
    if storage_uri:
        payload["storage_uri"] = storage_uri
        if backend == "minio":
            payload["minio_uri"] = str((source or {}).get("minio_uri") or storage_uri).strip() or storage_uri
        elif backend == "s3":
            payload["s3_uri"] = str((source or {}).get("s3_uri") or storage_uri).strip() or storage_uri
    return payload


def normalize_attachments(attachments: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(attachments, list):
        return normalized
    for item in attachments:
        if isinstance(item, str):
            storage = _normalize_storage_payload({"path": item})
            if storage:
                payload = {"kind": "file", "storage": storage}
                payload["rel_path"] = str(storage.get("path") or "").strip()
                payload.update(_build_storage_compatibility_fields(storage))
                normalized.append(payload)
                continue
            normalized.append({"kind": "file", "storage": {"path": item}})
            continue
        if not isinstance(item, dict):
            continue
        if "storage" in item and isinstance(item["storage"], dict):
            storage = _normalize_storage_payload(item["storage"])
            payload = {**item, "storage": storage}
            if storage and not str(payload.get("rel_path") or "").strip():
                payload["rel_path"] = str(storage.get("path") or "").strip()
            if not str(payload.get("original_file_name") or "").strip():
                payload["original_file_name"] = str(payload.get("name") or payload.get("file_name") or "").strip() or None
            payload.update(_build_storage_compatibility_fields(storage, source=item))
            normalized.append(payload)
            continue
        storage_uri = (
            str(item.get("storage_uri") or "").strip()
            or str(item.get("s3_uri") or "").strip()
            or str(item.get("minio_uri") or "").strip()
        )
        if storage_uri:
            storage = _normalize_storage_payload({"storage_uri": storage_uri})
            if storage:
                payload = {**item, "storage": storage}
                if not str(payload.get("rel_path") or "").strip():
                    payload["rel_path"] = str(storage.get("path") or "").strip()
                if not str(payload.get("original_file_name") or "").strip():
                    payload["original_file_name"] = str(payload.get("name") or payload.get("file_name") or "").strip() or None
                payload.update(_build_storage_compatibility_fields(storage, source=item))
                normalized.append(payload)
                continue
        if "rel_path" in item:
            storage = _normalize_storage_payload(
                {
                    "backend": item.get("storage_backend") or item.get("backend"),
                    "bucket": item.get("storage_bucket") or item.get("bucket"),
                    "path": item.get("rel_path"),
                }
            )
            payload = {
                "kind": item.get("kind", "file"),
                "name": item.get("name"),
                    "original_file_name": item.get("original_file_name") or item.get("name") or item.get("file_name"),
                "mime": item.get("mime"),
                "size": item.get("size"),
                "storage": storage or {"path": item.get("rel_path")},
                "checksum": item.get("checksum"),
                "download_url": item.get("download_url"),
                    "uploaded_at": item.get("uploaded_at") or item.get("upload_time") or item.get("timestamp"),
                "storage_backend": item.get("storage_backend"),
                "storage_bucket": item.get("storage_bucket"),
            }
            payload.update(_build_storage_compatibility_fields(storage, source=item))
            normalized.append(payload)
            continue
        normalized.append(item)
    return normalized


def _extract_attachment_path(attachment: dict[str, Any]) -> str | None:
    storage = attachment.get("storage")
    if isinstance(storage, dict):
        path = storage.get("path")
        if isinstance(path, str) and path:
            return path
    rel_path = attachment.get("rel_path")
    if isinstance(rel_path, str) and rel_path:
        return rel_path
    return None


def attachments_for_client(attachments: list[dict[str, Any]]) -> list[Any]:
    result: list[Any] = []
    for item in attachments:
        storage = item.get("storage")
        if isinstance(storage, dict):
            backend = storage.get("backend") or item.get("storage_backend")
            bucket = storage.get("bucket") or item.get("storage_bucket")
            storage_path = storage.get("path")
            storage_uri = str(storage.get("storage_uri") or item.get("storage_uri") or "").strip()
            if not storage_uri and isinstance(storage_path, str) and storage_path and isinstance(bucket, str) and bucket:
                storage_uri = build_storage_uri(backend, bucket, storage_path) or ""
            if storage_uri:
                result.append(storage_uri)
                continue
        path = _extract_attachment_path(item)
        if path:
            result.append(path)
            continue
        if isinstance(storage, dict):
            url = storage.get("url")
            if isinstance(url, str) and url:
                result.append(url)
                continue
        result.append(item)
    return result


def coerce_file_meta(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    normalized = normalize_attachments([item])
    if not normalized:
        return None
    attachment = normalized[0]
    path = _extract_attachment_path(attachment)
    if not path:
        return None
    file_meta: dict[str, Any] = {
        "rel_path": path,
        "name": attachment.get("original_file_name") or attachment.get("name"),
        "original_file_name": attachment.get("original_file_name") or attachment.get("name") or attachment.get("file_name"),
        "mime": attachment.get("mime"),
        "size": attachment.get("size"),
    }
    uploaded_at = attachment.get("uploaded_at") or attachment.get("upload_time") or attachment.get("timestamp")
    if isinstance(uploaded_at, str) and uploaded_at.strip():
        file_meta["uploaded_at"] = uploaded_at.strip()
    download_url = attachment.get("download_url")
    if isinstance(download_url, str) and download_url:
        file_meta["download_url"] = download_url
    storage_uri = str(
        attachment.get("storage_uri")
        or attachment.get("s3_uri")
        or attachment.get("minio_uri")
        or ""
    ).strip()
    if storage_uri:
        file_meta["storage_uri"] = storage_uri
    storage = attachment.get("storage")
    if isinstance(storage, dict):
        backend = storage.get("backend") or attachment.get("storage_backend")
        bucket = storage.get("bucket") or attachment.get("storage_bucket")
        if isinstance(backend, str) and backend:
            file_meta["storage_backend"] = normalize_storage_backend(backend)
        if isinstance(bucket, str) and bucket:
            file_meta["storage_bucket"] = bucket
        generated_storage_uri = build_storage_uri(backend, bucket, path)
        if generated_storage_uri:
            file_meta["storage_uri"] = generated_storage_uri
    storage_uri_value = str(file_meta.get("storage_uri") or "").strip()
    if storage_uri_value:
        backend, _bucket, _rel_path = parse_storage_uri(storage_uri_value)
        if backend == "minio":
            file_meta["minio_uri"] = storage_uri_value
        elif backend == "s3":
            file_meta["s3_uri"] = storage_uri_value
        # 说明：附件下载不在 MVP 范围内（MinIO 不可用），因此这里不再签发下载令牌。
        # 旧实现的 download_tokens 有已知缺陷（不绑定用户、24h TTL、硬编码兜底密钥），已随模块删除。
        # 若后续恢复附件能力，应签发绑定 user_id + file_id 的一次性令牌。
    return file_meta


def attachments_to_files(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for attachment in attachments:
        meta = coerce_file_meta(attachment)
        if meta:
            files.append(meta)
    return files


def build_envelope(
    event: str,
    source: str,
    body: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    parent_msg_id: str | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "msg_id": msg_id or f"m-{uuid.uuid4()}",
        "timestamp": int(time.time() * 1000),
        "source": source,
        "event": event,
    }
    if parent_msg_id:
        header["parent_msg_id"] = parent_msg_id
    metadata_dict = dict(metadata or {})
    trace_id = metadata_dict.get("trace_id") or metadata_dict.get("request_msg_id")
    if isinstance(trace_id, str) and trace_id.strip():
        header["trace_id"] = trace_id.strip()
    return {"header": header, "payload": body, "metadata": metadata_dict}


def decode_contract(raw_event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    if "payload" in raw_event and isinstance(raw_event["payload"], dict):
        body = raw_event["payload"]
        header = raw_event.get("header", {})
        metadata = raw_event.get("metadata", {})
        event_type = header.get("event") or body.get("event") or "message.reply"
        return body, header, metadata, str(event_type)
    body = raw_event
    header = {}
    metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
    status_event = metadata.get("status_event") if isinstance(metadata, dict) else None
    event_type = f"status.{status_event}" if status_event else "message.reply"
    return body, header, metadata, event_type


def resolve_tenant_id(tenant_id: Any, metadata: dict[str, Any] | None) -> str:
    if isinstance(tenant_id, str) and tenant_id.strip():
        return tenant_id.strip()
    if isinstance(metadata, dict):
        metadata_tenant = metadata.get("tenant_id")
        if isinstance(metadata_tenant, str) and metadata_tenant.strip():
            return metadata_tenant.strip()
    return DEFAULT_TENANT_ID


def coerce_interaction(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    interaction_type = str(value.get("type") or "").strip().lower()
    if interaction_type not in {"select", "otp", "confirm", "form"}:
        interaction_type = "form"
    title = str(value.get("title") or value.get("prompt") or "需要补充信息").strip()
    prompt = str(value.get("prompt") or value.get("message") or title).strip()
    if not title:
        title = "需要补充信息"
    if not prompt:
        prompt = title
    payload: dict[str, Any] = {
        "type": interaction_type,
        "title": title,
        "prompt": prompt,
    }
    for field_name in ("version", "interaction_id", "hint", "submit_label", "cancel_label"):
        field_value = str(value.get(field_name) or "").strip()
        if field_value:
            payload[field_name] = field_value
    expires_in_seconds = value.get("expires_in_seconds")
    if isinstance(expires_in_seconds, int):
        payload["expires_in_seconds"] = max(1, expires_in_seconds)
    payload["multi_select"] = bool(value.get("multi_select", False))
    payload["sensitive"] = bool(value.get("sensitive", False))
    options = value.get("options")
    if isinstance(options, list):
        payload["options"] = [item for item in options if isinstance(item, dict)]
    fields = value.get("fields")
    if isinstance(fields, list):
        payload["fields"] = [item for item in fields if isinstance(item, dict)]
    context = value.get("context")
    if isinstance(context, dict):
        payload["context"] = context
    response_values = value.get("values")
    if isinstance(response_values, dict):
        payload["values"] = response_values
    if interaction_type == "otp" and not payload.get("fields"):
        payload["fields"] = [
            {
                "key": "otp_code",
                "label": "验证码",
                "input_type": "otp",
                "required": True,
                "placeholder": "请输入验证码",
            }
        ]
        payload["sensitive"] = True
    if interaction_type == "confirm" and not payload.get("options"):
        payload["options"] = [{"label": "确认", "value": "yes"}, {"label": "取消", "value": "no"}]
    return payload


def _minio_region() -> str:
    return _env_first("APP_MINIO_REGION", "ICATMSG_MINIO_REGION", "MINIO_REGION", default="")


def _new_minio_client() -> S3CompatClient:
    endpoint = _minio_endpoint()
    access_key, secret_key = _minio_credentials()
    return S3CompatClient(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=_minio_secure(),
        region=_minio_region(),
    )


def get_minio_client() -> S3CompatClient:
    global _minio_client
    if _minio_client is None:
        _minio_client = _new_minio_client()
    return _minio_client


async def ensure_minio_bucket() -> str:
    bucket = _minio_bucket()
    if not bucket:
        raise RuntimeError("ICATMSG_MINIO_BUCKET is required")
    client = get_minio_client()
    exists = await asyncio.to_thread(client.bucket_exists, bucket)
    if not exists:
        await asyncio.to_thread(client.make_bucket, bucket)
    return bucket


async def upload_bytes_to_object_storage(rel_path: str, file_bytes: bytes, content_type: str) -> dict[str, Any]:
    bucket = await ensure_minio_bucket()
    client = get_minio_client()
    await asyncio.to_thread(
        client.put_bytes,
        bucket,
        rel_path,
        file_bytes,
        content_type or "application/octet-stream",
    )
    return {
        "rel_path": rel_path,
        "size": len(file_bytes),
        "mime": content_type or "application/octet-stream",
        "storage_backend": "minio",
        "storage_bucket": bucket,
        "storage_uri": f"minio://{bucket}/{rel_path}",
        "minio_uri": f"minio://{bucket}/{rel_path}",
    }


async def upload_bytes_to_minio(rel_path: str, file_bytes: bytes, content_type: str) -> dict[str, Any]:
    return await upload_bytes_to_object_storage(rel_path, file_bytes, content_type)


async def open_object_storage_download(path: str) -> tuple[Any, dict[str, Any]]:
    normalized_path = str(path or "").strip()
    if normalized_path.lower().startswith(("/api/", "api/")):
        # Assume platform backend API
        if not normalized_path.startswith("/"):
            normalized_path = "/" + normalized_path
        path = f"http://platform-backend:8000{normalized_path}"

    if path.lower().startswith(("http://", "https://")):
        # Proxy download from a URL
        client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)
        try:
            request_obj = client.build_request("GET", path)
            response = await client.send(request_obj, stream=True)
            response.raise_for_status()
            
            # Wrapper to ensure client is closed when stream is done
            class StreamWrapper:
                def __init__(self, resp, cli):
                    self.resp = resp
                    self.cli = cli
                def aiter_bytes(self, **kwargs):
                    return self.resp.aiter_bytes(**kwargs)
                async def aclose(self):
                    await self.resp.aclose()
                    await self.cli.aclose()

            return StreamWrapper(response, client), {
                "rel_path": os.path.basename(urlparse(path).path) or "download",
                "mime": response.headers.get("content-type", "application/octet-stream"),
                "size": int(response.headers.get("content-length", "0")),
                "is_proxy": True
            }
        except Exception:
            await client.aclose()
            raise

    _backend, bucket_override, normalized_path = split_storage_uri(path)
    if not normalized_path:
        raise FileNotFoundError("invalid path")
    client = get_minio_client()
    bucket = bucket_override or _minio_bucket()
    if not bucket:
        raise RuntimeError("ICATMSG_MINIO_BUCKET is required")
    response, full_resp = await asyncio.to_thread(client.get_object, bucket, normalized_path)
    return response, {
        "rel_path": normalized_path,
        "size": int(full_resp.get("ContentLength") or 0),
        "mime": str(full_resp.get("ContentType") or "application/octet-stream"),
        "storage_backend": "minio",
        "storage_bucket": bucket,
        "storage_uri": f"minio://{bucket}/{normalized_path}",
        "minio_uri": f"minio://{bucket}/{normalized_path}",
    }


async def open_minio_download(path: str) -> tuple[Any, dict[str, Any]]:
    return await open_object_storage_download(path)


async def _connect_kafka_producer() -> AIOKafkaProducer:
    global _kafka_producer
    if _kafka_producer is not None:
        return _kafka_producer
    servers = _kafka_bootstrap_servers()
    if not servers:
        raise RuntimeError("ICATMSG_KAFKA_BOOTSTRAP_SERVERS is required")
    producer = AIOKafkaProducer(bootstrap_servers=servers, **_build_kafka_auth_options())
    await producer.start()
    _kafka_producer = producer
    return producer


async def publish_inbound_message(envelope: dict[str, Any]) -> None:
    producer = await _connect_kafka_producer()
    inbound_topic, _ = _kafka_topics()
    payload_bytes = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    await producer.send_and_wait(inbound_topic, payload_bytes, key=_inbound_partition_key(envelope))


async def _connect_kafka_outbound_consumer() -> AIOKafkaConsumer:
    global _kafka_outbound_consumer
    if _kafka_outbound_consumer is not None:
        return _kafka_outbound_consumer
    servers = _kafka_bootstrap_servers()
    if not servers:
        raise RuntimeError("ICATMSG_KAFKA_BOOTSTRAP_SERVERS is required")
    _, outbound_topic = _kafka_topics()
    consumer = AIOKafkaConsumer(
        outbound_topic,
        bootstrap_servers=servers,
        group_id=_kafka_outbound_group_id(),
        enable_auto_commit=False,
        # D11=A：earliest + 手动提交，保证网关重启期间 agent 的回复不会丢
        auto_offset_reset="earliest",
        **_build_kafka_auth_options(),
    )
    await consumer.start()
    _kafka_outbound_consumer = consumer
    return consumer


async def _consume_outbound_messages(
    handler: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], str], Awaitable[None]],
) -> None:
    global _kafka_outbound_consumer
    global _outbound_consumer_last_error
    global _outbound_consumer_last_event_at_ms
    global _outbound_consumer_last_started_at_ms
    global _outbound_consumer_restart_count
    while True:
        consumer: AIOKafkaConsumer | None = None
        try:
            consumer = await _connect_kafka_outbound_consumer()
            _outbound_consumer_last_started_at_ms = _now_ms()
            _outbound_consumer_last_error = ""
            logger.info(
                "started outbound kafka consumer for topic=%s group_id=%s",
                _kafka_topics()[1],
                _kafka_outbound_group_id(),
            )
            async for record in consumer:
                _outbound_consumer_last_event_at_ms = _now_ms()
                should_commit = False
                try:
                    try:
                        raw_event = json.loads(record.value.decode("utf-8"))
                    except json.JSONDecodeError:
                        should_commit = True
                        logger.exception(
                            "failed to decode outbound kafka message offset=%s partition=%s; skipping malformed payload",
                            record.offset,
                            record.partition,
                        )
                        raw_event = None
                    if raw_event is None:
                        # D11=A 修复：旧实现在这里直接 continue，导致"跳过畸形消息"的分支
                        # 永远不会提交位点，毒消息会把消费卡住。这里显式提交后再跳过。
                        if should_commit:
                            try:
                                await consumer.commit()
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logger.exception(
                                    "failed to commit skipped malformed offset=%s partition=%s",
                                    record.offset,
                                    record.partition,
                                )
                        continue
                    try:
                        body, header, metadata, event_type = decode_contract(raw_event)
                    except ValueError:
                        should_commit = True
                        logger.exception(
                            "failed to decode outbound kafka contract offset=%s partition=%s; skipping invalid envelope",
                            record.offset,
                            record.partition,
                        )
                        continue
                    await handler(body, header, metadata, event_type)
                    should_commit = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "failed to process outbound kafka message offset=%s partition=%s",
                        record.offset,
                        record.partition,
                    )
                if should_commit:
                    try:
                        await consumer.commit()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "failed to commit outbound kafka offset=%s partition=%s",
                            record.offset,
                            record.partition,
                        )
        except asyncio.CancelledError:
            logger.info("stopped outbound kafka consumer loop")
            raise
        except Exception as exc:
            _outbound_consumer_restart_count += 1
            _outbound_consumer_last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("outbound kafka consumer failed; restarting")
            await asyncio.sleep(min(30, max(1, _outbound_consumer_restart_count)))
        finally:
            if consumer is not None:
                with suppress(Exception):
                    await consumer.stop()
            if _kafka_outbound_consumer is consumer:
                _kafka_outbound_consumer = None


async def start_outbound_consumer(
    handler: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], str], Awaitable[None]],
) -> asyncio.Task[None] | None:
    global _kafka_outbound_task
    if not _kafka_outbound_enabled():
        logger.info("outbound kafka consumer disabled by ICATMSG_KAFKA_OUTBOUND_ENABLED")
        return None
    if _kafka_outbound_task is not None and not _kafka_outbound_task.done():
        return _kafka_outbound_task
    _kafka_outbound_task = asyncio.create_task(_consume_outbound_messages(handler), name="icatmsg-outbound-consumer")
    return _kafka_outbound_task


async def stop_gateway_runtime() -> None:
    global _kafka_producer, _kafka_outbound_consumer, _kafka_outbound_task
    global _outbound_consumer_last_error, _outbound_consumer_last_started_at_ms, _outbound_consumer_last_event_at_ms
    task = _kafka_outbound_task
    consumer = _kafka_outbound_consumer
    producer = _kafka_producer
    _kafka_outbound_task = None
    _kafka_outbound_consumer = None
    _kafka_producer = None
    _outbound_consumer_last_error = ""
    _outbound_consumer_last_started_at_ms = 0
    _outbound_consumer_last_event_at_ms = 0

    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if consumer is not None:
        await consumer.stop()
    if producer is not None:
        await producer.stop()
