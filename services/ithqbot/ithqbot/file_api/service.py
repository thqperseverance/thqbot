"""Tenant-isolated file service built on MinIO/S3 compatible storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from pathlib import Path
from ithqbot.storage import (
    ObjectStorageClient,
    build_storage_session,
    normalize_storage_backend,
    parse_storage_uri,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_segment(value: str, *, fallback: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return fallback
    normalized = normalized.replace("\\", "_").replace("/", "_")
    normalized = re.sub(r"[^a-zA-Z0-9_.:-]", "_", normalized)
    return normalized or fallback


def _safe_filename(name: str) -> str:
    cleaned = _safe_segment(name, fallback="file.bin")
    return cleaned if "." in cleaned else f"{cleaned}.bin"


def _safe_prefix(value: str, *, fallback: str) -> str:
    normalized = str(value or "").strip().strip("/")
    if not normalized:
        return fallback.strip("/")
    segments = [_safe_segment(part, fallback="x") for part in normalized.split("/") if part.strip()]
    return "/".join(segments) if segments else fallback.strip("/")


@dataclass(slots=True)
class FileServiceConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    backend: str = "minio"
    secure: bool = False
    region: str = ""
    max_file_size_bytes: int = 10 * 1024 * 1024
    download_url_ttl_seconds: int = 600
    metadata_prefix: str = "_meta/files"


class FileService:
    """File storage service with strict tenant/account/bot isolation."""

    def __init__(
        self,
        *,
        config: FileServiceConfig,
        storage_client: ObjectStorageClient | None = None,
    ):
        self._config = config
        self._backend = normalize_storage_backend(config.backend)
        self._bucket = _safe_segment(config.bucket, fallback="ithqbot-storage")
        if storage_client is None:
            session = build_storage_session(
                None,
                bucket=self._bucket,
                fallback={
                    "backend": self._backend,
                    "endpoint": config.endpoint,
                    "access_key": config.access_key,
                    "secret_key": config.secret_key,
                    "bucket": self._bucket,
                    "secure": bool(config.secure),
                    "region": config.region,
                },
            )
            self._storage = session.client
        else:
            self._storage = storage_client

    @classmethod
    def from_runtime_config(cls, config: Any) -> "FileService":
        storage_cfg = config.get_active_storage_config() if config else None
        file_api_cfg = getattr(getattr(config, "tools", None), "file_api", None)
        service_cfg = FileServiceConfig(
            backend=str(getattr(storage_cfg, "backend", "minio") or "minio"),
            endpoint=str(getattr(storage_cfg, "endpoint", "localhost:9000") or "localhost:9000"),
            access_key=str(getattr(storage_cfg, "access_key", "") or ""),
            secret_key=str(getattr(storage_cfg, "secret_key", "") or ""),
            bucket=str(getattr(storage_cfg, "bucket", "ithqbot-storage") or "ithqbot-storage"),
            secure=bool(getattr(storage_cfg, "secure", False)),
            region=str(getattr(storage_cfg, "region", "") or ""),
            max_file_size_bytes=int(getattr(file_api_cfg, "max_file_size_bytes", 10 * 1024 * 1024)),
            download_url_ttl_seconds=int(getattr(file_api_cfg, "download_url_ttl_seconds", 600)),
            metadata_prefix=str(getattr(file_api_cfg, "metadata_prefix", "_meta/files") or "_meta/files"),
        )
        return cls(config=service_cfg)

    async def save(
        self,
        *,
        tenant_id: str,
        account_id: str,
        bot_id: str,
        chat_id: str,
        request_msg_id: str,
        trace_id: str,
        name: str,
        content: str | bytes,
        mime: str = "application/octet-stream",
        tags: list[str] | None = None,
        source_skill: str | None = None,
        expire_at: str | None = None,
    ) -> dict[str, Any]:
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if len(payload) > self._config.max_file_size_bytes:
            raise ValueError(f"file too large: {len(payload)} > {self._config.max_file_size_bytes}")

        scope = self._scope_ids(tenant_id, account_id, bot_id, chat_id, request_msg_id)
        original_file_name = str(name or "").strip() or "file.bin"
        file_name = _safe_filename(original_file_name)
        file_id = self._generate_file_id(scope["tenant_id"], scope["account_id"], scope["bot_id"], trace_id)
        object_key = (
            f"{scope['tenant_id']}/{scope['account_id']}/{scope['bot_id']}/"
            f"{scope['chat_id']}/{scope['request_msg_id']}/{file_id}/{file_name}"
        )
        created_at = _utcnow_iso()
        detected_mime = (
            mime if str(mime or "").strip() else (mimetypes.guess_type(original_file_name)[0] or "application/octet-stream")
        )
        await asyncio.to_thread(
            self._storage.put_object,
            self._bucket,
            object_key,
            payload,
            detected_mime,
        )
        metadata = {
            "schema_version": "v1",
            "file_id": file_id,
            "name": original_file_name,
            "original_file_name": original_file_name,
            "mime": detected_mime,
            "size": len(payload),
            "tenant_id": scope["tenant_id"],
            "account_id": scope["account_id"],
            "bot_id": scope["bot_id"],
            "chat_id": scope["chat_id"],
            "request_msg_id": scope["request_msg_id"],
            "trace_id": str(trace_id or "").strip(),
            "created_at": created_at,
            "uploaded_at": created_at,
            **self._build_storage_fields(bucket=self._bucket, object_key=object_key),
            "tags": [str(item) for item in (tags or []) if str(item).strip()],
            "source_skill": str(source_skill or "").strip(),
            "expire_at": str(expire_at or "").strip() or None,
        }
        await self._save_metadata(file_id=file_id, metadata=metadata)
        metadata["download_url"] = await self._build_download_url(object_key)
        return metadata

    async def get(
        self,
        *,
        file_id: str,
        tenant_id: str,
        account_id: str,
        bot_id: str,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        metadata = await self._get_metadata(file_id)
        self._assert_scope(metadata, tenant_id=tenant_id, account_id=account_id, bot_id=bot_id)
        metadata["download_url"] = await self._build_download_url(
            str(((metadata.get("storage") or {}).get("object_key") or ""))
        )
        if include_internal:
            return metadata
        return self._to_public_metadata(metadata)

    async def read(
        self,
        *,
        file_id: str,
        tenant_id: str,
        account_id: str,
        bot_id: str,
    ) -> bytes:
        chunks: list[bytes] = []
        async for chunk in self.read_stream(
            file_id=file_id,
            tenant_id=tenant_id,
            account_id=account_id,
            bot_id=bot_id,
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    async def read_stream(
        self,
        *,
        file_id: str,
        tenant_id: str,
        account_id: str,
        bot_id: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        metadata = await self._get_metadata(file_id)
        self._assert_scope(metadata, tenant_id=tenant_id, account_id=account_id, bot_id=bot_id)
        object_key = str(((metadata.get("storage") or {}).get("object_key") or ""))
        if not object_key:
            raise FileNotFoundError(f"file object key missing: {file_id}")
        response = await asyncio.to_thread(self._storage.get_object, self._bucket, object_key)
        try:
            while True:
                data = await asyncio.to_thread(response.read, max(1, int(chunk_size)))
                if not data:
                    break
                yield data
        finally:
            try:
                response.close()
            except Exception:
                pass
            try:
                response.release_conn()
            except Exception:
                pass

    async def list(
        self,
        *,
        tenant_id: str,
        account_id: str,
        bot_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        prefix = f"{_safe_prefix(self._config.metadata_prefix, fallback='_meta/files')}/"
        objects = await asyncio.to_thread(lambda: list(self._storage.list_objects(self._bucket, prefix)))
        results: list[dict[str, Any]] = []
        for obj in objects:
            object_name = str(getattr(obj, "object_name", "") or "")
            if not object_name.endswith(".json"):
                continue
            try:
                payload = await self._read_json_object(object_name)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if (
                str(payload.get("tenant_id") or "") != str(tenant_id)
                or str(payload.get("account_id") or "") != str(account_id)
                or str(payload.get("bot_id") or "") != str(bot_id)
            ):
                continue
            payload["download_url"] = await self._build_download_url(
                str(((payload.get("storage") or {}).get("object_key") or ""))
            )
            results.append(self._to_public_metadata(payload))
        results.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        normalized_limit = max(1, min(int(limit), 200))
        return results[:normalized_limit]

    async def resolve_file_id_from_legacy(
        self,
        *,
        file_entry: dict[str, Any],
        tenant_id: str,
        account_id: str,
        bot_id: str,
    ) -> str | None:
        file_id = str(file_entry.get("file_id") or "").strip()
        if file_id:
            try:
                await self.get(
                    file_id=file_id,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    bot_id=bot_id,
                )
                return file_id
            except Exception:
                return None
        backend, bucket, object_key = self._resolve_legacy_storage_target(file_entry)
        if not object_key:
            return None
        path_parts = [segment for segment in object_key.split("/") if segment]
        if len(path_parts) < 7:
            return await self._register_legacy_object_metadata(
                file_entry=file_entry,
                backend=backend,
                bucket=bucket,
                object_key=object_key,
                tenant_id=tenant_id,
                account_id=account_id,
                bot_id=bot_id,
            )
        maybe_tenant, maybe_account, maybe_bot = path_parts[0], path_parts[1], path_parts[2]
        if maybe_tenant != tenant_id or maybe_account != account_id or maybe_bot != bot_id:
            return await self._register_legacy_object_metadata(
                file_entry=file_entry,
                backend=backend,
                bucket=bucket,
                object_key=object_key,
                tenant_id=tenant_id,
                account_id=account_id,
                bot_id=bot_id,
            )
        derived_file_id = path_parts[-2]
        try:
            await self.get(
                file_id=derived_file_id,
                tenant_id=tenant_id,
                account_id=account_id,
                bot_id=bot_id,
            )
            return derived_file_id
        except Exception:
            return await self._register_legacy_object_metadata(
                file_entry=file_entry,
                backend=backend,
                bucket=bucket,
                object_key=object_key,
                tenant_id=tenant_id,
                account_id=account_id,
                bot_id=bot_id,
            )

    async def _save_metadata(self, *, file_id: str, metadata: dict[str, Any]) -> None:
        key = self._metadata_object_key(file_id)
        payload = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        await asyncio.to_thread(
            self._storage.put_object,
            self._bucket,
            key,
            payload,
            "application/json",
        )

    async def _get_metadata(self, file_id: str) -> dict[str, Any]:
        normalized = str(file_id or "").strip()
        if not normalized:
            raise FileNotFoundError("file_id is required")
        payload = await self._read_json_object(self._metadata_object_key(normalized))
        if not isinstance(payload, dict):
            raise FileNotFoundError(f"file metadata missing: {normalized}")
        return payload

    async def _read_json_object(self, object_key: str) -> dict[str, Any]:
        response = await asyncio.to_thread(self._storage.get_object, self._bucket, object_key)
        try:
            raw = await asyncio.to_thread(response.read)
        finally:
            try:
                response.close()
            except Exception:
                pass
            try:
                response.release_conn()
            except Exception:
                pass
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid metadata json: {object_key}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid metadata payload: {object_key}")
        return payload

    async def _build_download_url(self, object_key: str) -> str:
        if not object_key:
            return ""
        return await asyncio.to_thread(
            self._storage.presigned_get_object,
            self._bucket,
            object_key,
            self._config.download_url_ttl_seconds,
        )

    def _metadata_object_key(self, file_id: str) -> str:
        prefix = _safe_prefix(self._config.metadata_prefix, fallback="_meta/files")
        return f"{prefix}/{file_id}.json"

    def _legacy_file_id(self, *, bucket: str, object_key: str, tenant_id: str, account_id: str, bot_id: str) -> str:
        material = (
            f"legacy:{bucket}:{object_key}:{tenant_id}:{account_id}:{bot_id}:{self._config.secret_key}"
        ).encode("utf-8")
        return f"f_legacy_{hashlib.sha256(material).hexdigest()[:32]}"

    async def _register_legacy_object_metadata(
        self,
        *,
        file_entry: dict[str, Any],
        backend: str,
        bucket: str,
        object_key: str,
        tenant_id: str,
        account_id: str,
        bot_id: str,
    ) -> str | None:
        normalized_bucket = _safe_segment(bucket, fallback=self._bucket)
        normalized_object_key = str(object_key or "").strip().strip("/")
        if not normalized_object_key:
            return None
        file_id = self._legacy_file_id(
            bucket=normalized_bucket,
            object_key=normalized_object_key,
            tenant_id=tenant_id,
            account_id=account_id,
            bot_id=bot_id,
        )
        try:
            await self.get(
                file_id=file_id,
                tenant_id=tenant_id,
                account_id=account_id,
                bot_id=bot_id,
            )
            return file_id
        except Exception:
            pass
        filename = (
            str(file_entry.get("original_file_name") or file_entry.get("name") or file_entry.get("file_name") or "").strip()
            or Path(normalized_object_key).name
            or "legacy.bin"
        )
        mime = str(file_entry.get("mime") or "").strip() or (mimetypes.guess_type(filename)[0] or "application/octet-stream")
        size_value = file_entry.get("size")
        try:
            size = int(size_value or 0)
        except Exception:
            size = 0
        uploaded_at = str(file_entry.get("uploaded_at") or file_entry.get("upload_time") or file_entry.get("timestamp") or "").strip()
        created_at = uploaded_at or _utcnow_iso()
        metadata = {
            "schema_version": "v1",
            "file_id": file_id,
            "name": filename,
            "original_file_name": filename,
            "mime": mime,
            "size": max(0, size),
            "tenant_id": str(tenant_id or ""),
            "account_id": str(account_id or ""),
            "bot_id": str(bot_id or ""),
            "chat_id": "legacy-chat",
            "request_msg_id": "legacy-request",
            "trace_id": "",
            "created_at": created_at,
            "uploaded_at": created_at,
            **self._build_storage_fields(
                bucket=normalized_bucket,
                object_key=normalized_object_key,
                backend=backend,
            ),
            "tags": ["legacy-upload"],
            "source_skill": "legacy_attachment_import",
            "expire_at": None,
        }
        await self._save_metadata(file_id=file_id, metadata=metadata)
        return file_id

    def _resolve_legacy_storage_target(self, file_entry: dict[str, Any]) -> tuple[str, str, str]:
        storage = file_entry.get("storage")
        if isinstance(storage, dict):
            backend = normalize_storage_backend(storage.get("backend") or self._backend)
            bucket = str(storage.get("bucket") or self._bucket).strip()
            object_key = str(storage.get("object_key") or storage.get("path") or "").strip().strip("/")
            if object_key:
                return backend, bucket, object_key

        for key in ("storage_uri", "s3_uri", "minio_uri"):
            backend, bucket, object_key = parse_storage_uri(
                str(file_entry.get(key) or "").strip(),
                default_bucket=self._bucket,
            )
            if backend and object_key:
                return backend, bucket or self._bucket, object_key

        rel_path = str(file_entry.get("rel_path") or "").strip().strip("/")
        if rel_path:
            return self._backend, self._bucket, rel_path
        return self._backend, self._bucket, ""

    def _build_storage_fields(
        self,
        *,
        bucket: str,
        object_key: str,
        backend: str | None = None,
    ) -> dict[str, Any]:
        resolved_backend = normalize_storage_backend(backend or self._backend)
        normalized_bucket = str(bucket or self._bucket).strip() or self._bucket
        normalized_object_key = str(object_key or "").strip().strip("/")
        uri = f"{resolved_backend}://{normalized_bucket}/{normalized_object_key}"
        payload: dict[str, Any] = {
            "storage_uri": uri,
            "storage": {
                "backend": resolved_backend,
                "bucket": normalized_bucket,
                "path": normalized_object_key,
                "object_key": normalized_object_key,
            },
        }
        if resolved_backend == "s3":
            payload["s3_uri"] = uri
        else:
            payload["minio_uri"] = uri
        return payload

    def _generate_file_id(self, tenant_id: str, account_id: str, bot_id: str, trace_id: str) -> str:
        seed = uuid.uuid4().hex
        material = f"{seed}:{tenant_id}:{account_id}:{bot_id}:{trace_id}:{self._config.secret_key}".encode("utf-8")
        suffix = hashlib.sha256(material).hexdigest()[:20]
        return f"f_{seed}_{suffix}"

    @staticmethod
    def _scope_ids(
        tenant_id: str,
        account_id: str,
        bot_id: str,
        chat_id: str,
        request_msg_id: str,
    ) -> dict[str, str]:
        return {
            "tenant_id": _safe_segment(tenant_id, fallback="default_tenant"),
            "account_id": _safe_segment(account_id, fallback="anonymous"),
            "bot_id": _safe_segment(bot_id, fallback="default_bot"),
            "chat_id": _safe_segment(chat_id, fallback="default_chat"),
            "request_msg_id": _safe_segment(request_msg_id, fallback=f"req_{uuid.uuid4().hex[:12]}"),
        }

    @staticmethod
    def _assert_scope(
        metadata: dict[str, Any],
        *,
        tenant_id: str,
        account_id: str,
        bot_id: str,
    ) -> None:
        if (
            str(metadata.get("tenant_id") or "") != str(tenant_id)
            or str(metadata.get("account_id") or "") != str(account_id)
            or str(metadata.get("bot_id") or "") != str(bot_id)
        ):
            raise PermissionError("cross-tenant file access denied")

    @staticmethod
    def _to_public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "file_id": str(metadata.get("file_id") or ""),
            "name": str(metadata.get("original_file_name") or metadata.get("name") or ""),
            "original_file_name": str(metadata.get("original_file_name") or metadata.get("name") or ""),
            "mime": str(metadata.get("mime") or "application/octet-stream"),
            "size": int(metadata.get("size") or 0),
            "tenant_id": str(metadata.get("tenant_id") or ""),
            "account_id": str(metadata.get("account_id") or ""),
            "bot_id": str(metadata.get("bot_id") or ""),
            "chat_id": str(metadata.get("chat_id") or ""),
            "request_msg_id": str(metadata.get("request_msg_id") or ""),
            "trace_id": str(metadata.get("trace_id") or ""),
            "created_at": str(metadata.get("created_at") or ""),
            "uploaded_at": str(metadata.get("uploaded_at") or metadata.get("created_at") or ""),
            "download_url": str(metadata.get("download_url") or ""),
            "tags": list(metadata.get("tags") or []),
            "source_skill": str(metadata.get("source_skill") or ""),
            "expire_at": metadata.get("expire_at"),
        }


__all__ = [
    "FileService",
    "FileServiceConfig",
    "ObjectStorageClient",
]
