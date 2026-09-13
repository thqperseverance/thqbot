from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace

import pytest

from ithqbot.agent.loop import AgentLoop
from ithqbot.agent.skills.base import SkillContext
from ithqbot.file_api import FileService
from ithqbot.file_api.service import FileServiceConfig


class _MemoryObjectResponse:
    def __init__(self, data: bytes) -> None:
        self._buffer = BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        return

    def release_conn(self) -> None:
        return


@dataclass
class _StoredObject:
    object_name: str


class InMemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}

    def put_object(self, bucket: str, object_name: str, data: bytes, content_type: str) -> None:
        self.objects[(bucket, object_name)] = {"data": bytes(data), "content_type": content_type}

    def get_object(self, bucket: str, object_name: str):
        key = (bucket, object_name)
        if key not in self.objects:
            raise FileNotFoundError(object_name)
        payload = self.objects[key]
        return _MemoryObjectResponse(bytes(payload["data"]))  # type: ignore[arg-type]

    def list_objects(self, bucket: str, prefix: str):
        return [
            _StoredObject(object_name=object_name)
            for (obj_bucket, object_name), _payload in self.objects.items()
            if obj_bucket == bucket and object_name.startswith(prefix)
        ]

    def presigned_get_object(self, bucket: str, object_name: str, expires_seconds: int) -> str:
        return f"https://files.test/{bucket}/{object_name}?ttl={expires_seconds}&sig=ok"


def _build_service(
    storage: InMemoryStorage,
    *,
    backend: str = "minio",
    max_bytes: int = 1024 * 1024,
) -> FileService:
    return FileService(
        config=FileServiceConfig(
            backend=backend,
            endpoint="localhost:9000",
            access_key="ak",
            secret_key="sk",
            bucket="tenant-files",
            secure=False,
            max_file_size_bytes=max_bytes,
            download_url_ttl_seconds=600,
            metadata_prefix="_meta/files",
        ),
        storage_client=storage,
    )


@pytest.mark.asyncio
async def test_file_service_scope_and_path_constraints() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage)

    meta = await service.save(
        tenant_id="t-1",
        account_id="a-1",
        bot_id="b-1",
        chat_id="c-1",
        request_msg_id="m-1",
        trace_id="trace-1",
        name="report.md",
        content="hello",
        mime="text/markdown",
        tags=["ops", "daily"],
        source_skill="file_summary_skill",
        expire_at="2030-01-01T00:00:00Z",
    )
    file_id = str(meta["file_id"])
    object_key = str(meta["storage"]["object_key"])
    assert object_key.startswith("t-1/a-1/b-1/c-1/m-1/")
    assert f"/{file_id}/report.md" in object_key

    listed = await service.list(tenant_id="t-1", account_id="a-1", bot_id="b-1", limit=10)
    assert len(listed) == 1
    assert listed[0]["file_id"] == file_id
    assert listed[0]["name"] == "report.md"
    assert listed[0]["original_file_name"] == "report.md"
    assert listed[0]["uploaded_at"] == listed[0]["created_at"]
    assert listed[0]["tags"] == ["ops", "daily"]

    data = await service.read(file_id=file_id, tenant_id="t-1", account_id="a-1", bot_id="b-1")
    assert data == b"hello"

    with pytest.raises(PermissionError):
        await service.read(file_id=file_id, tenant_id="t-2", account_id="a-1", bot_id="b-1")


@pytest.mark.asyncio
async def test_file_service_s3_metadata_uses_storage_uri() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage, backend="s3")

    meta = await service.save(
        tenant_id="t-1",
        account_id="a-1",
        bot_id="b-1",
        chat_id="c-1",
        request_msg_id="m-1",
        trace_id="trace-1",
        name="report.md",
        content="hello",
        mime="text/markdown",
    )

    assert meta["storage_uri"].startswith("s3://tenant-files/")
    assert meta["s3_uri"] == meta["storage_uri"]
    assert "minio_uri" not in meta
    assert meta["storage"]["backend"] == "s3"
    assert meta["storage"]["bucket"] == "tenant-files"
    assert meta["storage"]["path"] == meta["storage"]["object_key"]


@pytest.mark.asyncio
async def test_file_service_rejects_oversized_file() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage, max_bytes=4)
    with pytest.raises(ValueError):
        await service.save(
            tenant_id="t",
            account_id="a",
            bot_id="b",
            chat_id="c",
            request_msg_id="m",
            trace_id="trace",
            name="big.bin",
            content=b"12345",
        )


@pytest.mark.asyncio
async def test_skill_context_file_api_and_permission() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage)
    ctx = SkillContext(
        tenant_id="tenant-x",
        account_id="acct-x",
        chat_id="chat-x",
        bot_id="bot-x",
        trace_id="trace-x",
        request_msg_id="req-x",
        skill_name="demo",
        file_service=service,
    )
    saved = await ctx.save_file("note.txt", "hello world", "text/plain")
    assert saved["name"] == "note.txt"
    file_id = str(saved["file_id"])

    content = await ctx.read_file(file_id)
    assert content == "hello world"

    info = await ctx.get_file(file_id)
    assert info["file_id"] == file_id
    assert info["tenant_id"] == "tenant-x"

    files = await ctx.list_files(limit=5)
    assert files and files[0]["file_id"] == file_id

    other_ctx = SkillContext(
        tenant_id="tenant-x",
        account_id="acct-x",
        chat_id="chat-x",
        bot_id="bot-y",
        file_service=service,
    )
    with pytest.raises(PermissionError):
        await other_ctx.read_file(file_id)


@pytest.mark.asyncio
async def test_agent_loop_legacy_file_payload_is_normalized() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage)
    meta = await service.save(
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
        chat_id="chat-1",
        request_msg_id="req-1",
        trace_id="trace-1",
        name="legacy.txt",
        content="legacy",
        mime="text/plain",
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.file_service = service

    payload = {
        "files": [
            {
                "name": "legacy.txt",
                "minio_uri": str(meta["minio_uri"]),
            }
        ]
    }
    skill_context = SimpleNamespace(tenant_id="tenant-1", account_id="account-1", bot_id="bot-1")
    normalized = await loop._normalize_payload_files(payload, skill_context=skill_context)
    assert isinstance(normalized, dict)
    assert normalized["files"] == [{"file_id": meta["file_id"]}]


@pytest.mark.asyncio
async def test_agent_loop_storage_uri_payload_is_normalized_for_s3() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage, backend="s3")
    meta = await service.save(
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
        chat_id="chat-1",
        request_msg_id="req-1",
        trace_id="trace-1",
        name="legacy.txt",
        content="legacy",
        mime="text/plain",
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.file_service = service

    payload = {
        "files": [
            {
                "name": "legacy.txt",
                "storage": {
                    "backend": "s3",
                    "bucket": "tenant-files",
                    "path": str(meta["storage"]["object_key"]),
                },
            }
        ]
    }
    skill_context = SimpleNamespace(tenant_id="tenant-1", account_id="account-1", bot_id="bot-1")
    normalized = await loop._normalize_payload_files(payload, skill_context=skill_context)
    assert isinstance(normalized, dict)
    assert normalized["files"] == [{"file_id": meta["file_id"]}]


@pytest.mark.asyncio
async def test_resolve_file_id_from_gateway_legacy_object_registers_metadata() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage)
    object_key = "u123/c1/abc123.docx"
    storage.put_object(
        "tenant-files",
        object_key,
        b"legacy docx bytes",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    file_id = await service.resolve_file_id_from_legacy(
        file_entry={
            "name": "1.docx",
            "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size": 16,
            "minio_uri": f"minio://tenant-files/{object_key}",
        },
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
    )

    assert isinstance(file_id, str) and file_id.startswith("f_legacy_")
    fetched = await service.get(
        file_id=file_id,
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
        include_internal=True,
    )
    assert fetched["storage"]["object_key"] == object_key
    assert fetched["name"] == "1.docx"


@pytest.mark.asyncio
async def test_file_service_public_metadata_keeps_original_file_name() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage)

    meta = await service.save(
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
        chat_id="chat-1",
        request_msg_id="req-1",
        trace_id="trace-1",
        name="活动通知.docx",
        content="docx-bytes",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    fetched = await service.get(
        file_id=str(meta["file_id"]),
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
    )

    assert fetched["name"] == "活动通知.docx"
    assert fetched["original_file_name"] == "活动通知.docx"
    assert fetched["uploaded_at"] == fetched["created_at"]


@pytest.mark.asyncio
async def test_resolve_file_id_from_storage_uri_registers_s3_metadata() -> None:
    storage = InMemoryStorage()
    service = _build_service(storage, backend="s3")
    object_key = "tenant-1/account-1/bot-1/chat-1/req-1/file-1/report.pdf"
    storage.put_object("tenant-files", object_key, b"legacy pdf bytes", "application/pdf")

    file_id = await service.resolve_file_id_from_legacy(
        file_entry={
            "name": "report.pdf",
            "mime": "application/pdf",
            "size": 16,
            "storage_uri": f"s3://tenant-files/{object_key}",
        },
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
    )

    assert isinstance(file_id, str) and file_id.startswith("f_legacy_")
    fetched = await service.get(
        file_id=file_id,
        tenant_id="tenant-1",
        account_id="account-1",
        bot_id="bot-1",
        include_internal=True,
    )
    assert fetched["storage_uri"] == f"s3://tenant-files/{object_key}"
    assert fetched["s3_uri"] == fetched["storage_uri"]
    assert fetched["storage"]["backend"] == "s3"
