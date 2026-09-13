"""Unified object storage helpers for MinIO and S3 backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ithqbot.storage.s3_compat import S3CompatibleClient

StorageBackend = Literal["minio", "s3"]


class ObjectStorageClient(Protocol):
    """Minimal object storage protocol shared across skills and tools."""

    def put_object(self, bucket: str, object_name: str, data: bytes, content_type: str) -> None:
        ...

    def fput_object(
        self,
        bucket: str,
        object_name: str,
        file_path: str,
        content_type: str | None = None,
    ) -> None:
        ...

    def get_object(self, bucket: str, object_name: str):
        ...

    def fget_object(self, bucket: str, object_name: str, file_path: str) -> None:
        ...

    def list_objects(self, bucket: str, prefix: str = "", recursive: bool = True):
        ...

    def presigned_get_object(self, bucket_name: str, object_name: str, expires: Any) -> str:
        ...

    def stat_object(self, bucket: str, object_name: str):
        ...


@dataclass(slots=True)
class ObjectStorageSettings:
    """Normalized runtime settings for the active object storage backend."""

    backend: StorageBackend = "minio"
    endpoint: str = "localhost:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "ithqbot-storage"
    secure: bool = False
    region: str = ""


@dataclass(slots=True)
class ObjectStorageSession:
    """Resolved object storage client and its active bucket/backend metadata."""

    client: ObjectStorageClient
    settings: ObjectStorageSettings
    bucket: str

    @property
    def backend(self) -> StorageBackend:
        return self.settings.backend

    def build_uri(self, object_path: str) -> str:
        normalized = str(object_path or "").strip().lstrip("/")
        return f"{self.backend}://{self.bucket}/{normalized}"

    def build_storage_metadata(self, object_path: str) -> dict[str, str]:
        normalized = str(object_path or "").strip().lstrip("/")
        return {
            "backend": self.backend,
            "bucket": self.bucket,
            "path": normalized,
        }

    def build_compatibility_fields(self, object_path: str) -> dict[str, str]:
        uri = self.build_uri(object_path)
        payload = {"storage_uri": uri}
        if self.backend == "minio":
            payload["minio_uri"] = uri
        elif self.backend == "s3":
            payload["s3_uri"] = uri
        return payload


def normalize_storage_backend(value: Any) -> StorageBackend:
    return "s3" if str(value or "").strip().lower() == "s3" else "minio"


def parse_storage_uri(uri: str, *, default_bucket: str = "") -> tuple[StorageBackend | None, str, str]:
    text = str(uri or "").strip()
    for scheme in ("minio://", "s3://"):
        if text.startswith(scheme):
            backend = normalize_storage_backend(scheme[:-3])
            body = text[len(scheme):]
            bucket, _, object_path = body.partition("/")
            return backend, bucket.strip() or default_bucket, object_path.strip().lstrip("/")
    return None, default_bucket, text


def is_storage_uri(value: str) -> bool:
    backend, _, object_path = parse_storage_uri(value)
    return backend is not None and bool(object_path)


def resolve_runtime_storage_settings(config: Any) -> ObjectStorageSettings:
    """Read the active storage config while remaining compatible with old MinIO fields."""

    storage_cfg = None
    getter = getattr(config, "get_active_storage_config", None)
    if callable(getter):
        storage_cfg = getter()
    if storage_cfg is None:
        getter = getattr(config, "get_active_minio_config", None)
        if callable(getter):
            storage_cfg = getter()
    if storage_cfg is None:
        tools_cfg = getattr(config, "tools", None)
        storage_cfg = getattr(tools_cfg, "storage", None) or getattr(tools_cfg, "minio", None)

    return ObjectStorageSettings(
        backend=normalize_storage_backend(getattr(storage_cfg, "backend", "minio")),
        endpoint=str(getattr(storage_cfg, "endpoint", "localhost:9000") or "localhost:9000").strip(),
        access_key=str(getattr(storage_cfg, "access_key", "") or "").strip(),
        secret_key=str(getattr(storage_cfg, "secret_key", "") or "").strip(),
        bucket=str(getattr(storage_cfg, "bucket", "ithqbot-storage") or "ithqbot-storage").strip(),
        secure=bool(getattr(storage_cfg, "secure", False)),
        region=str(getattr(storage_cfg, "region", "") or "").strip(),
    )


def create_object_storage_client(settings: ObjectStorageSettings) -> ObjectStorageClient:
    """Instantiate the concrete client for the configured backend."""

    return S3CompatibleClient(
        settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        secure=settings.secure,
        backend=settings.backend,
        region_name=settings.region or None,
    )


def build_storage_session(
    config: Any,
    *,
    bucket: str | None = None,
    fallback: dict[str, Any] | None = None,
) -> ObjectStorageSession:
    """Resolve runtime config, apply per-call fallbacks, and build a storage session."""

    settings = resolve_runtime_storage_settings(config)
    fallback = fallback or {}

    backend = normalize_storage_backend(fallback.get("backend", settings.backend))
    endpoint = str(fallback.get("endpoint", settings.endpoint) or "").strip()
    access_key = str(fallback.get("access_key", settings.access_key) or "").strip()
    secret_key = str(fallback.get("secret_key", settings.secret_key) or "").strip()
    region = str(fallback.get("region", settings.region) or "").strip()
    bucket_name = str(bucket or fallback.get("bucket", settings.bucket) or "").strip()
    secure = bool(fallback["secure"]) if "secure" in fallback else bool(settings.secure)

    normalized = ObjectStorageSettings(
        backend=backend,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket_name,
        secure=secure,
        region=region,
    )

    if not normalized.bucket:
        raise RuntimeError("未配置对象存储 bucket。")
    if normalized.backend != "s3" and not normalized.endpoint:
        raise RuntimeError("未配置对象存储 endpoint。")

    return ObjectStorageSession(
        client=create_object_storage_client(normalized),
        settings=normalized,
        bucket=normalized.bucket,
    )


__all__ = [
    "ObjectStorageClient",
    "ObjectStorageSession",
    "ObjectStorageSettings",
    "StorageBackend",
    "build_storage_session",
    "create_object_storage_client",
    "is_storage_uri",
    "normalize_storage_backend",
    "parse_storage_uri",
    "resolve_runtime_storage_settings",
]
