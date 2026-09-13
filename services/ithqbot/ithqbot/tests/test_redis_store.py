import uuid
from pathlib import Path

import pytest

from ithqbot import context
from ithqbot.session.manager import SessionManager
from ithqbot.session.redis_store import RedisSessionStore


class _FakeRedis:
    def __init__(self, store: dict[str, str] | None = None, fail_get_times: int = 0) -> None:
        self._store: dict[str, str] = store if store is not None else {}
        self._fail_get_times = fail_get_times

    def get(self, key: str) -> str | None:
        if self._fail_get_times > 0:
            self._fail_get_times -= 1
            raise RuntimeError("redis disconnected")
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def scan_iter(self, match: str):
        prefix = match[:-1] if match.endswith("*") else match
        for key in list(self._store.keys()):
            if key.startswith(prefix):
                yield key

    def lock(self, key: str, timeout=None, blocking_timeout=None, thread_local=True):
        _ = (timeout, blocking_timeout, thread_local)
        return _FakeRedisLock(key)


class _FakeRedisLock:
    def __init__(self, key: str) -> None:
        self.key = key
        self.acquired = False

    def acquire(self) -> bool:
        self.acquired = True
        return True

    def release(self) -> None:
        self.acquired = False


@pytest.fixture
def session_manager(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda _uri, decode_responses=True: fake)
    workspace = Path("/tmp/ithqbot-test-workspace")
    store = RedisSessionStore("redis://localhost:6379/0")
    return SessionManager(workspace=workspace, store=store)

@pytest.fixture
def tenant_context():
    test_tenant = "tenant_pytest_redis"
    test_account = "account_pytest_redis"
    
    token_tenant = context.tenant_id.set(test_tenant)
    token_account = context.account_id.set(test_account)
    
    yield test_tenant
    
    context.tenant_id.reset(token_tenant)
    context.account_id.reset(token_account)

def test_redis_session_store_crud(session_manager, tenant_context):
    _ = tenant_context
    test_key = f"cli_test_{uuid.uuid4().hex[:6]}"

    session = session_manager.get_or_create(test_key)
    session.add_message("user", "Hello, Pytest Redis Session!")
    session_manager.save(session)

    session_manager.invalidate(test_key)

    loaded_session = session_manager.get_or_create(test_key)
    assert len(loaded_session.messages) == 1
    assert loaded_session.messages[0]["content"] == "Hello, Pytest Redis Session!"

    sessions_list = session_manager.list_sessions()
    assert len(sessions_list) == 1
    found = any(s["key"] == test_key for s in sessions_list)
    assert found is True

    session_manager.store.delete(test_key)
    sessions_after_delete = session_manager.list_sessions()
    found_after_delete = any(s["key"] == test_key for s in sessions_after_delete)
    assert found_after_delete is False


def test_redis_session_store_reconnect_on_load_failure(monkeypatch):
    shared_store: dict[str, str] = {}
    first_client = _FakeRedis(store=shared_store, fail_get_times=1)
    second_client = _FakeRedis(store=shared_store)
    calls = {"count": 0}

    def _from_url(_uri, decode_responses=True):
        _ = decode_responses
        calls["count"] += 1
        if calls["count"] == 1:
            return first_client
        return second_client

    monkeypatch.setattr("redis.Redis.from_url", _from_url)

    store = RedisSessionStore("redis://localhost:6379/0")
    manager = SessionManager(workspace=Path("/tmp/ithqbot-test-workspace"), store=store)
    session = manager.get_or_create("reconnect_case")
    session.add_message("user", "hello")
    manager.save(session)
    manager.invalidate("reconnect_case")
    loaded = manager.get_or_create("reconnect_case")

    assert len(loaded.messages) == 1
    assert calls["count"] >= 2


def test_redis_session_manager_reloads_latest_state_across_instances(monkeypatch):
    shared_store: dict[str, str] = {}
    fake = _FakeRedis(store=shared_store)
    monkeypatch.setattr("redis.Redis.from_url", lambda _uri, decode_responses=True: fake)

    store_a = RedisSessionStore("redis://localhost:6379/0")
    store_b = RedisSessionStore("redis://localhost:6379/0")
    manager_a = SessionManager(workspace=Path("/tmp/ithqbot-test-workspace"), store=store_a)
    manager_b = SessionManager(workspace=Path("/tmp/ithqbot-test-workspace"), store=store_b)

    token_tenant = context.tenant_id.set("tenant_pytest_redis")
    token_account = context.account_id.set("account_pytest_redis")
    try:
        session_a = manager_a.get_or_create("shared_case")
        session_a.add_message("user", "from-a")
        manager_a.save(session_a)

        session_b = manager_b.get_or_create("shared_case")
        session_b.add_message("assistant", "from-b")
        manager_b.save(session_b)

        refreshed = manager_a.get_or_create("shared_case")
    finally:
        context.tenant_id.reset(token_tenant)
        context.account_id.reset(token_account)

    assert [message["content"] for message in refreshed.messages] == ["from-a", "from-b"]
