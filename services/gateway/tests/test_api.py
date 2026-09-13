"""HTTP API 的集成测试（TestClient + SQLite + 假 Redis）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import repository
from app.security import hash_password

BOB = {"username": "bob", "password": "bob-pass-123"}


def _login(client: TestClient, username: str, password: str) -> TestClient:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def _first_conversation_id(client: TestClient) -> str:
    response = client.get("/api/conversations")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload, "应至少有一个会话"
    return payload[0]["conversation_id"]


def test_login_success_and_me(client):
    response = client.post("/api/auth/login", json={"username": "alice", "password": "alice-pass-123"})
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "alice"
    assert "thqbot_session" in response.cookies or any(
        "session" in header.lower() for header in response.headers.get_list("set-cookie")
    )

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["display_name"] == "Alice"


def test_login_rejects_wrong_password_and_unknown_user(client):
    assert client.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401


def test_protected_endpoints_require_cookie(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/session/context").status_code == 401
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/stream").status_code == 401


def test_logout_clears_session(auth_client):
    assert auth_client.post("/api/auth/logout").status_code == 200
    assert auth_client.get("/api/auth/me").status_code == 401


def test_session_context_and_default_conversation(auth_client):
    context = auth_client.get("/api/session/context")
    assert context.status_code == 200
    body = context.json()
    assert body["user"]["username"] == "alice"
    assert body["bot_id"] == "bot_A"
    assert body["tenant_id"] == "test-tenant"

    conversations = auth_client.get("/api/conversations").json()
    assert len(conversations) == 1
    assert conversations[0]["unread_count"] == 0


def test_send_message_queues_and_persists(auth_client, inbound_calls):
    conversation_id = _first_conversation_id(auth_client)

    response = auth_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "你好，帮我总结一下", "client_msg_id": "u-fixed-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is True
    assert body["degraded"] is False
    assert body["message"]["sender"] == "user"
    assert body["message"]["request_msg_id"] == "u-fixed-1"

    # 信封已投递，且满足 ithqbot 的必填字段
    assert len(inbound_calls) == 1
    envelope = inbound_calls[0]
    assert envelope["header"]["event"] == "message.user"
    assert envelope["header"]["msg_id"] == "u-fixed-1"
    assert envelope["payload"]["chat_id"] == conversation_id
    assert envelope["payload"]["tenant_id"] == "test-tenant"
    assert envelope["payload"]["client_id"]
    assert envelope["payload"]["account_id"]

    messages = auth_client.get(f"/api/conversations/{conversation_id}/messages").json()
    assert [m["content"] for m in messages["messages"]] == ["你好，帮我总结一下"]


def test_send_message_degrades_when_kafka_unavailable(auth_client, monkeypatch):
    from app import orchestrator

    async def boom(_envelope):
        raise RuntimeError("kafka down")

    monkeypatch.setattr(orchestrator, "publish_inbound_message", boom)

    conversation_id = _first_conversation_id(auth_client)
    response = auth_client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "在吗"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is False
    assert body["degraded"] is True
    assert "kafka down" in (body["warning"] or "")

    messages = auth_client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert [m["sender"] for m in messages] == ["user", "bot"]
    assert messages[1]["status"] == "failed"


def test_unread_count_and_mark_read(auth_client, db_session, inbound_calls):
    """通过编排层直接写入一条 bot 消息，模拟 agent 回复。"""
    import asyncio

    from app import orchestrator

    conversation_id = _first_conversation_id(auth_client)
    conversation = repository.get_owned_conversation(
        db_session, user_id=auth_client.get("/api/session/context").json()["user"]["user_id"],
        conversation_id=conversation_id,
    )

    asyncio.run(
        orchestrator.handle_outbound_event(
            db_session,
            {"account_id": conversation.user_id, "chat_id": conversation_id, "content": "回复来了"},
            {"msg_id": "m-out-1", "event": "message.reply"},
            {"request_msg_id": "u-fixed-1"},
            "message.reply",
        )
    )

    conversations = auth_client.get("/api/conversations").json()
    assert conversations[0]["unread_count"] == 1

    read = auth_client.post(f"/api/conversations/{conversation_id}/read")
    assert read.status_code == 200
    assert read.json()["unread_count"] == 0
    assert auth_client.get("/api/conversations").json()[0]["unread_count"] == 0


def test_unknown_conversation_returns_404(auth_client):
    assert auth_client.get("/api/conversations/does-not-exist/messages").status_code == 404
    assert (
        auth_client.post("/api/conversations/does-not-exist/messages", json={"content": "x"}).status_code
        == 404
    )
    assert auth_client.post("/api/conversations/does-not-exist/read").status_code == 404


def test_conversation_isolation_between_users(client, db_session):
    """用户 B 不能读取/写入用户 A 的会话（D16/D17 的归属校验）。"""
    bob = repository.get_user_by_username(db_session, BOB["username"])
    if bob is None:
        bob = repository.create_user(
            db_session,
            username=BOB["username"],
            password_hash=hash_password(BOB["password"], iterations=1000),
            display_name="Bob",
        )
        db_session.commit()

    alice_client = _login(client, "alice", "alice-pass-123")
    alice_conversation_id = _first_conversation_id(alice_client)

    with TestClient(client.app) as bob_client:
        _login(bob_client, BOB["username"], BOB["password"])
        bob_conversation_id = _first_conversation_id(bob_client)
        assert bob_conversation_id != alice_conversation_id

        assert bob_client.get(f"/api/conversations/{alice_conversation_id}/messages").status_code == 404
        assert (
            bob_client.post(
                f"/api/conversations/{alice_conversation_id}/messages", json={"content": "hack"}
            ).status_code
            == 404
        )
        # B 的会话列表里看不到 A 的会话
        ids = [c["conversation_id"] for c in bob_client.get("/api/conversations").json()]
        assert alice_conversation_id not in ids


def test_delete_messages(auth_client, inbound_calls):
    conversation_id = _first_conversation_id(auth_client)
    auth_client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "第一条"})
    messages = auth_client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    target = messages[0]["message_id"]

    response = auth_client.request(
        "DELETE",
        f"/api/conversations/{conversation_id}/messages",
        json={"message_ids": [target]},
    )
    assert response.status_code == 200
    assert response.json()["deleted"] == 1
    remaining = auth_client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert remaining == []


def test_create_conversation(auth_client):
    # 先触发默认会话创建，这样新建的才是"第二个"
    assert auth_client.get("/api/session/context").status_code == 200
    assert len(auth_client.get("/api/conversations").json()) == 1

    response = auth_client.post("/api/conversations", json={"title": "新话题"})
    assert response.status_code == 201
    assert response.json()["title"] == "新话题"
    assert len(auth_client.get("/api/conversations").json()) == 2


def test_health_and_ready(auth_client):
    assert auth_client.get("/health").json()["status"] == "ok"
    ready = auth_client.get("/ready")
    # Kafka 在测试中被禁用，只检查 db + redis（假实现返回 ok）
    assert ready.status_code == 200
    checks = ready.json()["checks"]
    assert checks["database"]["ok"] is True
    assert checks["redis"]["ok"] is True


def test_runtime_config_requires_cookie(client):
    assert client.get("/api/config").status_code == 401


def test_runtime_config_defaults(auth_client):
    response = auth_client.get("/api/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bot_id"] == "bot_A"
    assert body["tenant_id"] == "test-tenant"
    assert body["model"]  # 默认 deepseek-flash
    assert body["models"][0] == body["model"], "当前模型必须排在清单首位"
    assert body["workspace_restricted"] is True
    assert body["max_upload_mb"] == 20


def test_runtime_config_model_list_is_ordered_and_deduped(auth_client, monkeypatch):
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("APP_LLM_MODELS", " deepseek-reasoner , deepseek-flash ,, ")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        body = auth_client.get("/api/config").json()
        assert body["models"] == ["deepseek-flash", "deepseek-reasoner"]
        assert body["model"] == "deepseek-flash"
    finally:
        get_settings.cache_clear()


def test_runtime_config_upload_limit_follows_max_upload_bytes(auth_client, monkeypatch):
    monkeypatch.setenv("APP_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert auth_client.get("/api/config").json()["max_upload_mb"] == 5
    finally:
        get_settings.cache_clear()

