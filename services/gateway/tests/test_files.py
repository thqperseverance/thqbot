"""附件（MinIO）链路测试。

对象存储用内存假实现替换，因此不需要 MinIO/网络；
归属校验、大小限制、路径安全、信封/消息 meta 传播都在这里覆盖。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app import object_store, repository
from app.security import hash_password

BOB = {"username": "bob", "password": "bob-pass-123"}


class _FakeStream:
    """模拟 boto3 StreamingBody：只需要同步 read()。"""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:  # pragma: no cover - 仅验证被调用
        self._buffer.close()


class _FakeObjectStore:
    def __init__(self, bucket: str = "test-bucket") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.uploaded: list[tuple[str, str]] = []

    def install(self, monkeypatch) -> None:
        async def ensure_bucket() -> str:
            return self.bucket

        async def put_object(*, object_path: str, data: bytes, content_type: str):
            self.objects[object_path] = data
            self.uploaded.append((object_path, content_type))
            return object_store.StoredObject(
                bucket=self.bucket,
                object_path=object_path,
                storage_uri=f"minio://{self.bucket}/{object_path}",
                size=len(data),
                mime=content_type or "application/octet-stream",
            )

        async def open_object(object_path: str):
            if object_path not in self.objects:
                raise FileNotFoundError(object_path)
            data = self.objects[object_path]
            return _FakeStream(data), {"mime": "application/octet-stream", "size": len(data)}

        async def delete_object(object_path: str) -> None:
            self.objects.pop(object_path, None)
            self.deleted.append(object_path)

        monkeypatch.setattr(object_store, "is_configured", lambda: True)
        monkeypatch.setattr(object_store, "ensure_bucket", ensure_bucket)
        monkeypatch.setattr(object_store, "put_object", put_object)
        monkeypatch.setattr(object_store, "open_object", open_object)
        monkeypatch.setattr(object_store, "delete_object", delete_object)


@pytest.fixture()
def object_store_fake(monkeypatch) -> _FakeObjectStore:
    fake = _FakeObjectStore()
    fake.install(monkeypatch)
    return fake


def _conversation_id(client: TestClient) -> str:
    response = client.get("/api/conversations")
    assert response.status_code == 200, response.text
    return response.json()[0]["conversation_id"]


def _upload(client: TestClient, conversation_id: str, name: str, payload: bytes, mime: str = "text/plain"):
    return client.post(
        f"/api/conversations/{conversation_id}/files",
        files={"file": (name, payload, mime)},
    )


# ------------------------------------------------------------------ 路径安全（纯函数）


@pytest.mark.parametrize(
    "raw,forbidden",
    [
        ("../../etc/passwd", ".."),
        ("..\\..\\windows\\system32\\cmd.exe", ".."),
        ("/absolute/path/file.txt", ".."),
        ("dir/sub/file.md", ".."),
    ],
)
def test_sanitize_filename_strips_directories_and_traversal(raw: str, forbidden: str):
    cleaned = object_store.sanitize_filename(raw)
    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert not cleaned.startswith(".")
    assert forbidden not in cleaned.split(".")[0]


def test_sanitize_filename_falls_back_for_empty_input():
    assert object_store.sanitize_filename("") == "upload.bin"
    assert object_store.sanitize_filename("...") == "upload.bin"


def test_build_object_path_contains_owner_and_file_id():
    path, file_id = object_store.build_object_path(
        user_id="user-1", conversation_id="conv-9", filename="report.md", file_id="abc123"
    )
    assert path == "uploads/user-1/conv-9/abc123.md"
    assert file_id == "abc123"


def test_build_object_path_handles_missing_conversation_and_extension():
    path, file_id = object_store.build_object_path(
        user_id="user-1", conversation_id=None, filename="noext"
    )
    assert path == f"uploads/user-1/unassigned/{file_id}"
    assert len(file_id) == 32


# ------------------------------------------------------------------ 上传


def test_upload_requires_login(client):
    response = client.post(
        "/api/conversations/whatever/files", files={"file": ("a.txt", b"hi", "text/plain")}
    )
    assert response.status_code == 401


def test_upload_returns_metadata_and_stores_object(auth_client, object_store_fake):
    conversation_id = _conversation_id(auth_client)
    response = _upload(auth_client, conversation_id, "report.md", b"# hello\n", "text/markdown")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "report.md"
    assert body["size"] == 8
    assert body["mime"] == "text/markdown"
    assert body["storage_uri"].startswith("minio://test-bucket/uploads/")
    assert body["download_url"] == f"/api/files/{body['file_id']}"

    stored_path = body["storage_uri"].split("test-bucket/", 1)[1]
    assert object_store_fake.objects[stored_path] == b"# hello\n"
    # 对象键必须包含归属用户，便于审计
    assert f"/{body['file_id']}." in stored_path


def test_upload_to_foreign_conversation_returns_404(client, db_session, object_store_fake):
    bob = repository.get_user_by_username(db_session, BOB["username"])
    if bob is None:
        bob = repository.create_user(
            db_session,
            username=BOB["username"],
            password_hash=hash_password(BOB["password"], iterations=1000),
            display_name="Bob",
        )
        db_session.commit()

    alice = client.post("/api/auth/login", json={"username": "alice", "password": "alice-pass-123"})
    assert alice.status_code == 200
    conversation_id = _conversation_id(client)

    with TestClient(client.app) as bob_client:
        bob_client.post("/api/auth/login", json={"username": BOB["username"], "password": BOB["password"]})
        response = _upload(bob_client, conversation_id, "steal.txt", b"x", "text/plain")
        assert response.status_code == 404


def test_upload_rejects_empty_file(auth_client, object_store_fake):
    conversation_id = _conversation_id(auth_client)
    response = _upload(auth_client, conversation_id, "empty.txt", b"")
    assert response.status_code == 400


def test_upload_rejects_oversized_file(auth_client, object_store_fake):
    from app.config import get_settings

    monkeypatch_target = get_settings()
    original = monkeypatch_target.max_upload_bytes
    monkeypatch_target.max_upload_bytes = 4
    try:
        conversation_id = _conversation_id(auth_client)
        response = _upload(auth_client, conversation_id, "big.txt", b"12345")
        assert response.status_code == 413
    finally:
        monkeypatch_target.max_upload_bytes = original


def test_upload_without_object_store_configured_returns_503(auth_client):
    conversation_id = _conversation_id(auth_client)
    response = _upload(auth_client, conversation_id, "a.txt", b"data")
    assert response.status_code == 503


def test_list_conversation_files(auth_client, object_store_fake):
    conversation_id = _conversation_id(auth_client)
    _upload(auth_client, conversation_id, "one.txt", b"1")
    _upload(auth_client, conversation_id, "two.txt", b"2")

    response = auth_client.get(f"/api/conversations/{conversation_id}/files")
    assert response.status_code == 200
    names = sorted(item["name"] for item in response.json())
    assert names == ["one.txt", "two.txt"]


# ------------------------------------------------------------------ 下载 / 删除


def test_download_returns_object_bytes(auth_client, object_store_fake):
    conversation_id = _conversation_id(auth_client)
    file_id = _upload(auth_client, conversation_id, "报告.md", "# 内容", "text/markdown").json()["file_id"]

    response = auth_client.get(f"/api/files/{file_id}")
    assert response.status_code == 200
    assert response.content == "# 内容".encode()
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition


def test_download_unknown_file_returns_404(auth_client, object_store_fake):
    assert auth_client.get("/api/files/deadbeef").status_code == 404


def test_download_foreign_file_returns_404(client, db_session, object_store_fake):
    """越权下载必须被拒（D17=A 的核心验收点）。"""
    bob = repository.get_user_by_username(db_session, BOB["username"])
    if bob is None:
        bob = repository.create_user(
            db_session,
            username=BOB["username"],
            password_hash=hash_password(BOB["password"], iterations=1000),
            display_name="Bob",
        )
        db_session.commit()

    client.post("/api/auth/login", json={"username": "alice", "password": "alice-pass-123"})
    conversation_id = _conversation_id(client)
    file_id = _upload(client, conversation_id, "secret.txt", b"top secret").json()["file_id"]

    with TestClient(client.app) as bob_client:
        bob_client.post("/api/auth/login", json={"username": BOB["username"], "password": BOB["password"]})
        assert bob_client.get(f"/api/files/{file_id}").status_code == 404
        assert bob_client.delete(f"/api/files/{file_id}").status_code == 404


def test_delete_removes_record_and_object(auth_client, object_store_fake, db_session):
    conversation_id = _conversation_id(auth_client)
    file_id = _upload(auth_client, conversation_id, "gone.txt", b"bye").json()["file_id"]

    response = auth_client.delete(f"/api/files/{file_id}")
    assert response.status_code == 200
    assert response.json()["object_removed"] is True
    assert object_store_fake.deleted, "应调用对象存储删除"
    assert auth_client.get(f"/api/files/{file_id}").status_code == 404


# ------------------------------------------------------------------ 消息携带附件


def test_send_message_with_attachment_propagates_to_envelope_and_meta(
    auth_client, object_store_fake, inbound_calls
):
    conversation_id = _conversation_id(auth_client)
    upload = _upload(auth_client, conversation_id, "spec-v1.md", b"# v1", "text/markdown").json()

    response = auth_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "请对比这两份文档", "file_ids": [upload["file_id"]]},
    )
    assert response.status_code == 200, response.text
    message = response.json()["message"]
    attachments = message["meta"]["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["name"] == "spec-v1.md"
    assert attachments[0]["storage_uri"].startswith("minio://test-bucket/uploads/")

    envelope = inbound_calls[0]
    payload = envelope["payload"]
    assert payload["attachments"][0]["storage_uri"] == upload["storage_uri"]
    assert payload["file_meta"]["name"] == "spec-v1.md"
    assert envelope["metadata"]["attachments"][0]["name"] == "spec-v1.md"


def test_send_message_allows_attachment_without_text(auth_client, object_store_fake, inbound_calls):
    conversation_id = _conversation_id(auth_client)
    upload = _upload(auth_client, conversation_id, "only-file.txt", b"data", "text/plain").json()

    response = auth_client.post(
        f"/api/conversations/{conversation_id}/messages", json={"file_ids": [upload["file_id"]]}
    )
    assert response.status_code == 200, response.text
    assert inbound_calls[0]["payload"]["content_type"] == "file"


def test_send_message_requires_content_or_attachment(auth_client, object_store_fake):
    conversation_id = _conversation_id(auth_client)
    response = auth_client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "  "})
    assert response.status_code == 422


def test_send_message_rejects_foreign_attachment(client, db_session, object_store_fake):
    bob = repository.get_user_by_username(db_session, BOB["username"])
    if bob is None:
        bob = repository.create_user(
            db_session,
            username=BOB["username"],
            password_hash=hash_password(BOB["password"], iterations=1000),
            display_name="Bob",
        )
        db_session.commit()

    client.post("/api/auth/login", json={"username": "alice", "password": "alice-pass-123"})
    conversation_id = _conversation_id(client)
    file_id = _upload(client, conversation_id, "a.txt", b"data").json()["file_id"]

    with TestClient(client.app) as bob_client:
        bob_client.post("/api/auth/login", json={"username": BOB["username"], "password": BOB["password"]})
        bob_conversation = bob_client.get("/api/conversations").json()[0]["conversation_id"]
        response = bob_client.post(
            f"/api/conversations/{bob_conversation}/messages",
            json={"content": "看看这个", "file_ids": [file_id]},
        )
        assert response.status_code == 400
        assert "附件" in response.json()["detail"]


def test_send_message_rejects_unknown_attachment(auth_client, object_store_fake):
    conversation_id = _conversation_id(auth_client)
    response = auth_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "x", "file_ids": ["no-such-file"]},
    )
    assert response.status_code == 400
