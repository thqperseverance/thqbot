"""pytest 公共夹具。

原则：单元测试必须**不依赖外部服务**。
- 数据库：临时 SQLite 文件（生产是 PostgreSQL，模型用 JSON().with_variant 兼容两者）；
- Redis：用内存假实现替换 app.realtime 的全部副作用函数；
- Kafka：APP_KAFKA_ENABLED=false，测试里直接 monkeypatch publish_inbound_message。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

GATEWAY_ROOT = Path(__file__).resolve().parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

TEST_USERNAME = "alice"
TEST_PASSWORD = "alice-pass-123"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """隔离环境变量、重建引擎与 schema。"""
    db_file = tmp_path / "thqbot-test.db"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+pysqlite:///{db_file}")
    monkeypatch.setenv("APP_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("APP_KAFKA_ENABLED", "false")
    monkeypatch.setenv("APP_AGENT_TENANT_ID", "test-tenant")
    monkeypatch.setenv("APP_AGENT_BOT_ID", "bot_A")
    monkeypatch.setenv("APP_CLIENT_ID", "thqbot-web-test")
    monkeypatch.setenv(
        "APP_AUTH_USERS",
        '[{"username":"%s","password":"%s","display_name":"Alice"}]' % (TEST_USERNAME, TEST_PASSWORD),
    )

    from app import config, db

    config.get_settings.cache_clear()
    db.reset_engine()

    import app.models  # noqa: F401  确保所有表已注册到 metadata
    from app.db import Base, get_engine

    Base.metadata.create_all(get_engine())

    yield

    config.get_settings.cache_clear()
    db.reset_engine()


class FakeRedisState:
    def __init__(self) -> None:
        self.keys: set[tuple[str, str]] = set()
        self.published: list[tuple[str, dict]] = []
        self.status: dict[tuple[str, str], dict] = {}
        self.ping_ok = True

    def events_of_type(self, event_type: str) -> list[dict]:
        return [event for _uid, event in self.published if event.get("type") == event_type]


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch) -> FakeRedisState:
    """把 app.realtime 的副作用替换为内存实现。"""
    from app import realtime

    state = FakeRedisState()

    async def claim_external_id(user_id, external_id, *, ttl_seconds=None):
        if not external_id:
            return True
        key = (user_id, external_id)
        if key in state.keys:
            return False
        state.keys.add(key)
        return True

    async def release_external_id(user_id, external_id):
        state.keys.discard((user_id, external_id))

    async def publish_event(user_id, event):
        state.published.append((user_id, event))

    async def cache_status(user_id, conversation_id, payload):
        state.status[(user_id, conversation_id)] = payload

    async def get_cached_status(user_id, conversation_id):
        return state.status.get((user_id, conversation_id))

    async def clear_status(user_id, conversation_id):
        state.status.pop((user_id, conversation_id), None)

    async def ping():
        return state.ping_ok

    monkeypatch.setattr(realtime, "claim_external_id", claim_external_id)
    monkeypatch.setattr(realtime, "release_external_id", release_external_id)
    monkeypatch.setattr(realtime, "publish_event", publish_event)
    monkeypatch.setattr(realtime, "cache_status", cache_status)
    monkeypatch.setattr(realtime, "get_cached_status", get_cached_status)
    monkeypatch.setattr(realtime, "clear_status", clear_status)
    monkeypatch.setattr(realtime, "ping", ping)
    return state


@pytest.fixture()
def db_session():
    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def seeded_user(db_session):
    """创建并返回测试用户。"""
    from app import repository
    from app.security import hash_password

    user = repository.get_user_by_username(db_session, TEST_USERNAME)
    if user is None:
        user = repository.create_user(
            db_session,
            username=TEST_USERNAME,
            password_hash=hash_password(TEST_PASSWORD, iterations=1000),
            display_name="Alice",
        )
        db_session.commit()
    return user


@pytest.fixture()
def default_conversation(db_session, seeded_user):
    from app import repository

    conversation = repository.get_or_create_default_conversation(
        db_session, user_id=seeded_user.id, bot_id="bot_A"
    )
    db_session.commit()
    return conversation


@pytest.fixture()
def inbound_calls(monkeypatch) -> list[dict]:
    """拦截 Kafka 入站投递，记录信封。"""
    from app import orchestrator

    calls: list[dict] = []

    async def fake_publish(envelope):
        calls.append(envelope)

    monkeypatch.setattr(orchestrator, "publish_inbound_message", fake_publish)
    return calls


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_client(client):
    """已登录的 TestClient。"""
    response = client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text
    return client
