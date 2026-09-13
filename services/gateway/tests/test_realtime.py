"""Redis 辅助层的单元测试。

注意：conftest 里的 autouse ``fake_redis`` 夹具会替换 ``app.realtime`` 上的
副作用函数，所以这里在**模块导入期**先抓住原始实现，再用一个内存假客户端去测它。
"""

from __future__ import annotations

import asyncio

import pytest

from app import realtime as realtime_module

# 在夹具 patch 之前抓取真实实现
_real_claim_external_id = realtime_module.claim_external_id
_real_release_external_id = realtime_module.release_external_id
_real_cache_status = realtime_module.cache_status
_real_get_cached_status = realtime_module.get_cached_status
_real_publish_event = realtime_module.publish_event
_real_ping = realtime_module.ping


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiry: dict[str, int | None] = {}
        self.published: list[tuple[str, str]] = []
        self.ping_ok = True

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.expiry[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        self.expiry.pop(key, None)

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        return 1

    async def ping(self):
        if not self.ping_ok:
            raise ConnectionError("down")
        return True


@pytest.fixture()
def fake_client(monkeypatch) -> _FakeRedis:
    client = _FakeRedis()
    monkeypatch.setattr(realtime_module, "get_redis", lambda: client)
    return client


def test_claim_external_id_uses_setnx_with_ttl(fake_client):
    assert asyncio.run(_real_claim_external_id("u1", "m1")) is True
    assert asyncio.run(_real_claim_external_id("u1", "m1")) is False
    assert asyncio.run(_real_claim_external_id("u1", "m2")) is True
    assert asyncio.run(_real_claim_external_id("u2", "m1")) is True  # 按用户隔离

    key = realtime_module.dedupe_key("u1", "m1")
    assert fake_client.expiry[key] == realtime_module.get_settings().dedupe_ttl_seconds
    assert fake_client.expiry[key] > 0, "去重键必须有 TTL（修复旧实现的无界增长）"


def test_claim_external_id_without_id_is_noop(fake_client):
    assert asyncio.run(_real_claim_external_id("u1", "")) is True
    assert fake_client.store == {}


def test_release_external_id_allows_retry(fake_client):
    assert asyncio.run(_real_claim_external_id("u1", "m1")) is True
    asyncio.run(_real_release_external_id("u1", "m1"))
    assert asyncio.run(_real_claim_external_id("u1", "m1")) is True


def test_status_cache_roundtrip(fake_client):
    asyncio.run(_real_cache_status("u1", "c1", {"stage": "processing", "skills": ["summarize"]}))
    cached = asyncio.run(_real_get_cached_status("u1", "c1"))
    assert cached == {"stage": "processing", "skills": ["summarize"]}
    assert asyncio.run(_real_get_cached_status("u1", "other")) is None
    key = realtime_module.status_key("u1", "c1")
    assert fake_client.expiry[key] == realtime_module.get_settings().status_cache_ttl_seconds


def test_status_cache_ignores_corrupted_payload(fake_client):
    fake_client.store[realtime_module.status_key("u1", "c1")] = "{not json"
    assert asyncio.run(_real_get_cached_status("u1", "c1")) is None


def test_publish_event_serializes_to_channel(fake_client):
    asyncio.run(_real_publish_event("u1", {"type": "message", "conversation_id": "c1"}))
    assert len(fake_client.published) == 1
    channel, payload = fake_client.published[0]
    assert channel == realtime_module.events_channel("u1")
    assert '"type": "message"' in payload


def test_ping_reports_failure(fake_client):
    assert asyncio.run(_real_ping()) is True
    fake_client.ping_ok = False
    assert asyncio.run(_real_ping()) is False


def test_key_formats_are_namespaced():
    assert realtime_module.dedupe_key("u", "m") == "thqbot:dedupe:u:m"
    assert realtime_module.events_channel("u") == "thqbot:events:u"
    assert realtime_module.status_key("u", "c") == "thqbot:status:u:c"
    assert realtime_module.lock_key("c") == "thqbot:lock:conversation:c"
