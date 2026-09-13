"""附件：上传 / 下载 / 列表 / 删除。

归属校验一律基于 PostgreSQL 里的 ``files`` 记录（``user_id`` + ``file_id``），
**不使用**"对象路径前缀"这类弱校验 —— 后者在旧网关里正是越权下载的成因。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import object_store, repository, schemas
from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..models import User

router = APIRouter(prefix="/api", tags=["files"])

CHUNK_SIZE = 64 * 1024


def _require_object_store() -> None:
    if not object_store.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="对象存储未配置，附件功能不可用",
        )


def _content_disposition(filename: str) -> str:
    """同时给出 ASCII 回退名与 RFC 5987 的 UTF-8 名。"""
    ascii_fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in filename) or "download.bin"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


async def _iter_object(stream: Any) -> AsyncIterator[bytes]:
    """兼容 httpx / boto3 StreamingBody / minio-py 三种流对象。"""
    if hasattr(stream, "aiter_bytes"):
        async for chunk in stream.aiter_bytes():
            yield chunk
        return
    if hasattr(stream, "iter_chunks"):
        for chunk in stream.iter_chunks(chunk_size=CHUNK_SIZE):
            if chunk:
                yield chunk
        return
    if hasattr(stream, "stream"):
        for chunk in stream.stream(CHUNK_SIZE):
            if chunk:
                yield chunk
        return
    while True:
        chunk = await asyncio.to_thread(stream.read, CHUNK_SIZE)
        if not chunk:
            break
        yield chunk


@router.post(
    "/conversations/{conversation_id}/files",
    response_model=schemas.FileOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    conversation_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> schemas.FileOut:
    _require_object_store()
    settings = get_settings()

    try:
        conversation = repository.get_owned_conversation(
            session, user_id=user.id, conversation_id=conversation_id
        )
    except repository.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件内容为空")
    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过上限 {limit_mb} MB",
        )

    filename = object_store.sanitize_filename(file.filename or "upload.bin")
    mime = (file.content_type or "").strip() or "application/octet-stream"
    file_id = uuid.uuid4().hex
    object_path, _ = object_store.build_object_path(
        user_id=user.id, conversation_id=conversation.id, filename=filename, file_id=file_id
    )

    try:
        stored = await object_store.put_object(object_path=object_path, data=data, content_type=mime)
    except object_store.ObjectStoreUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - 透出可读的存储错误
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"写入对象存储失败：{exc}"
        )

    record = repository.create_file_record(
        session,
        file_id=file_id,
        user_id=user.id,
        conversation_id=conversation.id,
        name=filename,
        mime=stored.mime,
        size=stored.size,
        bucket=stored.bucket,
        object_path=stored.object_path,
        storage_uri=stored.storage_uri,
    )
    session.commit()
    return schemas.FileOut(**repository.serialize_file(record))


@router.get("/conversations/{conversation_id}/files", response_model=list[schemas.FileOut])
def list_files(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> list[schemas.FileOut]:
    try:
        repository.get_owned_conversation(session, user_id=user.id, conversation_id=conversation_id)
    except repository.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    records = repository.list_conversation_files(
        session, user_id=user.id, conversation_id=conversation_id, limit=limit
    )
    return [schemas.FileOut(**repository.serialize_file(record)) for record in records]


@router.get("/files/{file_id}")
async def download_file(
    file_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> StreamingResponse:
    _require_object_store()
    try:
        record = repository.get_owned_file(session, user_id=user.id, file_id=file_id)
    except repository.NotFound:
        # 不区分"不存在"与"不属于你"，避免探测他人文件
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    try:
        stream, _meta = await object_store.open_object(record.object_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"读取对象存储失败：{exc}"
        )

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in _iter_object(stream):
                yield chunk
        finally:
            for closer in ("aclose", "close", "release_conn"):
                handler = getattr(stream, closer, None)
                if handler is None:
                    continue
                try:
                    result = handler()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    pass

    headers = {"Content-Disposition": _content_disposition(record.name)}
    if record.size:
        headers["Content-Length"] = str(record.size)
    return StreamingResponse(body(), media_type=record.mime or "application/octet-stream", headers=headers)


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        record = repository.get_owned_file(session, user_id=user.id, file_id=file_id)
    except repository.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    object_removed = True
    if object_store.is_configured():
        try:
            await object_store.delete_object(record.object_path)
        except Exception:  # noqa: BLE001 - 对象已丢失也要允许清理元数据
            object_removed = False

    repository.delete_file_record(session, record)
    session.commit()
    return {"status": "ok", "file_id": file_id, "object_removed": object_removed}
