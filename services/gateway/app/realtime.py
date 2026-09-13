"""Redis 用途（对应 D7=A）：去重、SSE 推送通道、进度状态缓存、分布式锁。

Redis 不再是会话/消息的事实来源 —— 它只承担：
- ``thqbot:dedupe:*``     消费幂等（带 TTL，修复旧实现无界增长的问题）
- ``thqbot:events:*``     Pub/Sub，用于 SSE 扇出
- ``thqbot:status:*``     最近一次进度状态（带 TTL），供历史消息回填"调用了哪个 skill"
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from redis import asyncio as redis_async

from .config import get_settings

STATE_PREFIX = "thqbot"
_RETRYABLE = (redis_async.RedisError, OSError, ValueError)

_client: redis_async.Redis | None = None


def get_redis() -> redis_async.Redis:
    global _client
    if _client is None:
        _client = redis_async.from_url(
            get_settings().redis_uri,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
    return _client


async def reset_redis() -> None:
    """测试/关闭时释放客户端。"""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # pragma: no cover - 关闭失败不影响主流程
            pass
    _client = None


# --------------------------------------------------------------------------- key


def dedupe_key(user_id: str, external_id: str) -> str:
    return f"{STATE_PREFIX}:dedupe:{user_id}:{external_id}"


def events_channel(user_id: str) -> str:
    return f"{STATE_PREFIX}:events:{user_id}"


def status_key(user_id: str, conversation_id: str) -> str:
    return f"{STATE_PREFIX}:status:{user_id}:{conversation_id}"


def lock_key(conversation_id: str) -> str:
    return f"{STATE_PREFIX}:lock:conversation:{conversation_id}"


# --------------------------------------------------------------------------- 操作


async def claim_external_id(user_id: str, external_id: str, *, ttl_seconds: int | None = None) -> bool:
    """SET NX + EX：首次见到返回 True，重复投递返回 False。"""
    if not external_id:
        return True
    ttl = ttl_seconds or get_settings().dedupe_ttl_seconds
    result = await get_redis().set(dedupe_key(user_id, external_id), "1", nx=True, ex=ttl)
    return bool(result)


async def release_external_id(user_id: str, external_id: str) -> None:
    """处理失败时释放占位，便于 Kafka 重投。"""
    if not external_id:
        return
    try:
        await get_redis().delete(dedupe_key(user_id, external_id))
    except _RETRYABLE:
        pass


async def publish_event(user_id: str, event: dict[str, Any]) -> None:
    """把事件广播到该用户的所有 SSE 连接。"""
    payload = json.dumps(event, ensure_ascii=False, default=str)
    await get_redis().publish(events_channel(user_id), payload)


async def cache_status(user_id: str, conversation_id: str, payload: dict[str, Any]) -> None:
    ttl = get_settings().status_cache_ttl_seconds
    await get_redis().set(
        status_key(user_id, conversation_id),
        json.dumps(payload, ensure_ascii=False, default=str),
        ex=ttl,
    )


async def get_cached_status(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    raw = await get_redis().get(status_key(user_id, conversation_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def clear_status(user_id: str, conversation_id: str) -> None:
    """新的一轮对话开始时清空累计进度，保证技能展示是按轮的。"""
    await get_redis().delete(status_key(user_id, conversation_id))


async def subscribe_user_events(user_id: str) -> AsyncIterator[dict[str, Any]]:
    """订阅用户事件通道，逐条 yield 反序列化后的事件字典。"""
    client = get_redis()
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(events_channel(user_id))
    try:
        while True:
            message = await pubsub.get_message(timeout=1.0)
            if not message:
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            if not isinstance(data, str):
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event
    finally:
        try:
            await pubsub.unsubscribe(events_channel(user_id))
        finally:
            await pubsub.aclose()


async def ping() -> bool:
    try:
        await get_redis().ping()
        return True
    except _RETRYABLE:
        return False
