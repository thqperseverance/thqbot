"""对象存储（MinIO / S3）封装。

职责边界：
- 本模块只做"字节进 / 字节出"，不关心业务归属；归属校验在 repository + routes 里做。
- 底层复用 ``source_adapter`` 已有的 S3 兼容客户端与 bucket 自动创建逻辑，
  避免再造一套连接管理。

对象键布局（**必须包含 user_id**，便于按前缀审计）::

    uploads/{user_id}/{conversation_id}/{file_id}{ext}

与 ithqbot 共用同一个 bucket，因此 ithqbot 侧技能（如 ``doc_compare``）可以直接用
信封里带过去的 ``minio://bucket/<object_path>`` 读取文件，无需额外拷贝。
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import uuid
from dataclasses import dataclass
from typing import Any

from . import source_adapter
from .config import get_settings

SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
UPLOAD_PREFIX = "uploads"


class ObjectStoreUnavailable(RuntimeError):
    """未配置对象存储（endpoint / bucket / 凭据缺失）。"""


@dataclass(slots=True)
class StoredObject:
    bucket: str
    object_path: str
    storage_uri: str
    size: int
    mime: str


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.minio_endpoint and settings.minio_bucket and settings.minio_access_key)


def _require_configured() -> None:
    if not is_configured():
        raise ObjectStoreUnavailable(
            "对象存储未配置：需要 APP_MINIO_ENDPOINT / APP_MINIO_BUCKET / APP_MINIO_ACCESS_KEY"
        )


def sanitize_filename(raw: str, *, fallback: str = "upload.bin") -> str:
    """只保留安全字符，去掉目录成分，长度截断。"""
    name = posixpath.basename(str(raw or "").replace("\\", "/")).strip()
    name = SAFE_NAME.sub("_", name).lstrip(".") or fallback
    return name[:180]


def build_object_path(
    *,
    user_id: str,
    conversation_id: str | None,
    filename: str,
    file_id: str | None = None,
) -> tuple[str, str]:
    """返回 ``(object_path, file_id)``。

    传入 ``file_id`` 时直接用它，使**存储路径与数据库里的文件 id 一一对应**，便于排查。
    """
    resolved_id = file_id or uuid.uuid4().hex
    safe_name = sanitize_filename(filename)
    _stem, dot, ext = safe_name.rpartition(".")
    extension = f".{ext}" if dot and 0 < len(ext) <= 12 else ""
    conversation_segment = conversation_id or "unassigned"
    object_path = f"{UPLOAD_PREFIX}/{user_id}/{conversation_segment}/{resolved_id}{extension}"
    return object_path, resolved_id


async def ensure_bucket() -> str:
    _require_configured()
    return await source_adapter.ensure_minio_bucket()


async def put_object(*, object_path: str, data: bytes, content_type: str) -> StoredObject:
    _require_configured()
    meta = await source_adapter.upload_bytes_to_object_storage(object_path, data, content_type)
    return StoredObject(
        bucket=str(meta.get("storage_bucket") or get_settings().minio_bucket),
        object_path=str(meta.get("rel_path") or object_path),
        storage_uri=str(meta.get("storage_uri") or ""),
        size=int(meta.get("size") or len(data)),
        mime=str(meta.get("mime") or content_type or "application/octet-stream"),
    )


async def open_object(object_path: str) -> tuple[Any, dict[str, Any]]:
    """返回 ``(stream, meta)``；meta 至少含 ``mime`` / ``size`` / ``name``。"""
    _require_configured()
    bucket = get_settings().minio_bucket
    return await source_adapter.open_object_storage_download(f"minio://{bucket}/{object_path}")


async def delete_object(object_path: str) -> None:
    _require_configured()
    bucket = get_settings().minio_bucket
    client = source_adapter.get_minio_client()
    await asyncio.to_thread(client.delete_object, bucket, object_path)


def build_storage_uri(object_path: str) -> str:
    return f"minio://{get_settings().minio_bucket}/{object_path}"
