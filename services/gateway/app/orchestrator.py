"""编排层：把「浏览器发消息」与「消费 agent 回复」串起来。

数据流（D11=A 的最小可靠改造）：
    浏览器 → POST /api/conversations/{id}/messages
      → PG 落库（用户消息）
      → Kafka ``icatmsg_inbound``（信封 v1.2，分区键 tenant|bot|account|chat）
      → ithqbot AgentLoop（消费、执行 skills）
      → Kafka ``icatmsg_outbound``（status.processing / message.reply / message.interaction）
      → 本模块消费：Redis 去重 → PG 落库（bot 消息）→ Redis Pub/Sub → SSE → 浏览器

与旧实现的差别：
- 出站消费改为 ``auto_offset_reset=earliest`` + Redis SETNX 去重 + 唯一约束兜底，
  agent 在网关重启期间的回复不会再丢。
- 进度（status.processing）不落无关消息行，而是写进 Redis 状态缓存，回填到最终回复的
  ``meta.progress``，于是历史消息里也能看到"调用了哪个 skill"（D14=A / D15=A）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy.orm import Session

from . import realtime, repository
from .config import get_settings
from .models import Conversation, User
from .source_adapter import build_envelope, publish_inbound_message

PROGRESS_KEYS = (
    "skill_name",
    "tool_name",
    "call_type",
    "progress_stage",
    "stage",
    "node_id",
    "graph_id",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_request_id() -> str:
    return f"u-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- 进度提取


def collect_progress_fields(payload: Any, *, max_depth: int = 6) -> dict[str, list[str]]:
    """递归收集进度载荷里的 skill/tool/stage 名称。

    对 ithqbot 的 ``_status_details`` 结构保持宽容：只要是 dict/list 就继续下钻，
    命中已知键就收集去重后的字符串值。
    """
    found: dict[str, list[str]] = {}

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PROGRESS_KEYS and isinstance(value, str) and value.strip():
                    bucket = found.setdefault(key, [])
                    if value.strip() not in bucket:
                        bucket.append(value.strip())
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload, 0)
    return found


def _dedupe_str(*values: Any) -> list[str]:
    """按出现顺序去重，过滤非字符串与空值。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        candidates = value if isinstance(value, (list, tuple, set)) else [value]
        for item in candidates:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def build_status_summary(*, body: dict[str, Any], metadata: dict[str, Any], event_type: str) -> dict[str, Any]:
    """把一次出站事件压缩成前端可展示的进度摘要。"""
    details = metadata.get("_status_details")
    fields = collect_progress_fields(
        {
            "status_details": details,
            "body": body,
            "metadata": {
                "progress_stage": metadata.get("_progress_stage"),
                "call_type": metadata.get("_progress_kind"),
            },
        }
    )
    percent = metadata.get("_progress_percent")
    return {
        "event": event_type,
        "stage": metadata.get("_progress_stage") or (fields.get("progress_stage") or [None])[0],
        "percent": percent if isinstance(percent, int) else None,
        "message": (body.get("content") or "")[:500],
        # ithqbot 把技能/工具名放在一等字段 `_skill_name` / `_tool_name` / `_call_type` 上；
        # 递归搜索（fields）只作为其它载荷形态的兜底。
        "skills": _dedupe_str(metadata.get("_skill_name"), fields.get("skill_name", [])),
        "tools": _dedupe_str(metadata.get("_tool_name"), fields.get("tool_name", [])),
        "call_types": _dedupe_str(metadata.get("_call_type"), fields.get("call_type", [])),
        "updated_at": _now_iso(),
    }


def merge_status(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """合并同一轮的多次进度事件。

    阶段与百分比取最新值；技能与工具名取并集 —— 否则最后那个不带工具信息的
    ``finalizing`` 事件会把前面暴露出来的 skill/tool 覆盖掉（技能就"看不见"了）。
    """
    previous = previous or {}
    merged = dict(current)
    merged["skills"] = _dedupe_str(previous.get("skills"), current.get("skills"))
    merged["tools"] = _dedupe_str(previous.get("tools"), current.get("tools"))
    merged["call_types"] = _dedupe_str(previous.get("call_types"), current.get("call_types"))
    if not merged.get("stage"):
        merged["stage"] = previous.get("stage")
    if merged.get("percent") is None:
        merged["percent"] = previous.get("percent")
    return merged


# --------------------------------------------------------------------------- 入站


def build_inbound_envelope(
    *,
    user: User,
    conversation: Conversation,
    content: str,
    request_msg_id: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    tenant_id = settings.agent_tenant_id
    attachment_items = list(attachments or [])
    body: dict[str, Any] = {
        "channel": "icatmsg",
        "account_id": user.id,
        "tenant_id": tenant_id,
        "bot_id": conversation.bot_id,
        "client_id": settings.client_id,
        "chat_id": conversation.id,
        "content": content,
        # 只有附件没有文字时标成 file，便于渠道/技能区分
        "content_type": "file" if attachment_items and not content.strip() else "text",
        "attachments": attachment_items,
        "file_meta": attachment_items[0] if attachment_items else None,
        "reply_to": None,
        "priority": "normal",
    }
    metadata: dict[str, Any] = {
        "tenant_id": tenant_id,
        "client_id": settings.client_id,
        "account_id": user.id,
        "bot_id": conversation.bot_id,
        "request_msg_id": request_msg_id,
        "trace_id": request_msg_id,
        "channel": "icatmsg",
    }
    if attachment_items:
        # ithqbot 的 doc_compare 直路由会从消息 metadata 的 attachments 里取路径，
        # 这里同时放进 metadata 作为冗余（渠道侧本来也会把 payload.attachments 提升上去）。
        metadata["attachments"] = attachment_items
    return build_envelope(
        event="message.user",
        source="thqbot-gateway",
        body=body,
        metadata=metadata,
        msg_id=request_msg_id,
    )


async def send_user_message(
    session: Session,
    *,
    user: User,
    conversation: Conversation,
    content: str,
    client_msg_id: str | None = None,
    file_ids: list[str] | None = None,
) -> dict[str, Any]:
    """落库用户消息（含附件）并投递到 Kafka；投递失败时写入一条 failed 的 bot 消息。

    ``file_ids`` 会被逐一做归属校验（不存在或不属于当前用户 -> :class:`repository.NotFound`）。
    """
    content = (content or "").strip()
    requested_ids = [str(item).strip() for item in (file_ids or []) if str(item or "").strip()]
    if not content and not requested_ids:
        raise ValueError("content or file_ids is required")

    attachments: list[dict[str, Any]] = []
    if requested_ids:
        records = repository.list_owned_files(session, user_id=user.id, file_ids=requested_ids)
        attachments = [repository.build_attachment_payload(record) for record in records]

    request_msg_id = (client_msg_id or "").strip() or new_request_id()

    meta: dict[str, Any] = {"request_msg_id": request_msg_id}
    if attachments:
        meta["attachments"] = attachments

    user_message = repository.append_message(
        session,
        conversation=conversation,
        sender="user",
        content=content,
        status="sent",
        request_msg_id=request_msg_id,
        external_id=f"inbound:{request_msg_id}",
        meta=meta,
    )
    # 只有附件时用文件名兜底作为会话标题
    title_source = content or (str(attachments[0].get("name") or "") if attachments else "")
    repository.rename_conversation_if_default(session, conversation, title_source)
    session.commit()
    # 新一轮开始：清空上一轮累计的技能/工具进度，保证展示按轮隔离
    await _safe_clear_status(user.id, conversation.id)

    serialized = repository.serialize_message(user_message) if user_message is not None else None
    if serialized is not None:
        await _safe_publish(
            user.id,
            {"type": "message", "conversation_id": conversation.id, "message": serialized},
        )

    envelope = build_inbound_envelope(
        user=user,
        conversation=conversation,
        content=content,
        request_msg_id=request_msg_id,
        attachments=attachments,
    )
    try:
        await publish_inbound_message(envelope)
    except Exception as exc:  # noqa: BLE001 - 需要把失败原因返回给前端
        warning = f"消息未能投递到 agent：{type(exc).__name__}: {exc}"
        failed = repository.append_message(
            session,
            conversation=conversation,
            sender="bot",
            content=warning,
            status="failed",
            request_msg_id=request_msg_id,
            reply_to=request_msg_id,
            meta={"error": str(exc), "request_msg_id": request_msg_id},
        )
        session.commit()
        payload = repository.serialize_message(failed) if failed is not None else None
        if payload is not None:
            await _safe_publish(
                user.id,
                {"type": "message", "conversation_id": conversation.id, "message": payload},
            )
        return {
            "message": serialized,
            "queued": False,
            "degraded": True,
            "warning": warning,
            "request_msg_id": request_msg_id,
        }

    return {
        "message": serialized,
        "queued": True,
        "degraded": False,
        "warning": None,
        "request_msg_id": request_msg_id,
    }


async def _safe_publish(user_id: str, event: dict[str, Any]) -> None:
    """Redis 不可用时不能让主链路失败（推送是可选的加速通道）。"""
    try:
        await realtime.publish_event(user_id, event)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- 出站


async def handle_outbound_event(
    session: Session,
    body: dict[str, Any],
    header: dict[str, Any],
    metadata: dict[str, Any],
    event_type: str,
) -> dict[str, Any] | None:
    """处理一条来自 ithqbot 的出站事件。"""
    user_id = str(body.get("account_id") or "").strip()
    conversation_id = str(body.get("chat_id") or "").strip()
    if not user_id or not conversation_id:
        return None

    if event_type == "status.processing":
        delta = build_status_summary(body=body, metadata=metadata, event_type=event_type)
        summary = merge_status(await _safe_get_status(user_id, conversation_id), delta)
        await _safe_cache_status(user_id, conversation_id, summary)
        await _safe_publish(
            user_id,
            {
                "type": "status",
                "conversation_id": conversation_id,
                "request_msg_id": metadata.get("request_msg_id"),
                "status": summary,
            },
        )
        return None

    external_id = str(header.get("msg_id") or "").strip() or None
    if external_id:
        try:
            first_time = await realtime.claim_external_id(user_id, external_id)
        except Exception:  # noqa: BLE001 - Redis 不可用时退化为依赖 DB 唯一约束
            first_time = True
        if not first_time:
            return None

    try:
        conversation = repository.get_owned_conversation(
            session, user_id=user_id, conversation_id=conversation_id
        )
    except repository.NotFound:
        if external_id:
            await _safe_release(user_id, external_id)
        return None

    content = body.get("content") or ""
    interaction = body.get("interaction") if isinstance(body.get("interaction"), dict) else None
    files = body.get("files") if isinstance(body.get("files"), list) else None
    attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else None

    if not content and not interaction and not files and not attachments:
        # 空回复（例如纯进度残留）不落库
        if external_id:
            await _safe_release(user_id, external_id)
        return None

    meta: dict[str, Any] = {
        "request_msg_id": metadata.get("request_msg_id") or header.get("parent_msg_id"),
        "trace_id": metadata.get("trace_id"),
        "event": event_type,
    }
    # 端到端耗时：本轮用户消息落库时间 -> 现在
    request_msg_id_value = meta.get("request_msg_id")
    if isinstance(request_msg_id_value, str) and request_msg_id_value:
        origin = repository.find_user_message_by_request_id(
            session, conversation_id=conversation_id, request_msg_id=request_msg_id_value
        )
        if origin is not None and origin.created_at is not None:
            started = origin.created_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            if elapsed_ms >= 0:
                meta["latency_ms"] = elapsed_ms
    # 真实 token 用量（由 ithqbot 侧在回合结束时写入 metadata.usage）
    usage = metadata.get("usage")
    if isinstance(usage, dict):
        meta["usage"] = usage
    if interaction:
        meta["interaction"] = interaction
    if files:
        meta["files"] = files
    if attachments:
        meta["attachments"] = attachments
    cached = await _safe_get_status(user_id, conversation_id)
    if cached:
        meta["progress"] = cached

    request_msg_id = meta.get("request_msg_id")
    message = repository.append_message(
        session,
        conversation=conversation,
        sender="bot",
        content=content,
        content_type=str(body.get("content_type") or "text"),
        status="received",
        request_msg_id=str(request_msg_id) if request_msg_id else None,
        reply_to=str(body.get("reply_to") or "") or None,
        external_id=external_id,
        meta=meta,
        increment_unread=True,
    )
    if message is None:
        # DB 唯一约束说这是重复投递
        session.commit()
        return None
    session.commit()

    serialized = repository.serialize_message(message)
    await _safe_publish(
        user_id,
        {"type": "message", "conversation_id": conversation_id, "message": serialized},
    )
    return serialized


async def _safe_cache_status(user_id: str, conversation_id: str, summary: dict[str, Any]) -> None:
    try:
        await realtime.cache_status(user_id, conversation_id, summary)
    except Exception:  # noqa: BLE001
        pass


async def _safe_get_status(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    try:
        return await realtime.get_cached_status(user_id, conversation_id)
    except Exception:  # noqa: BLE001
        return None


async def _safe_release(user_id: str, external_id: str) -> None:
    try:
        await realtime.release_external_id(user_id, external_id)
    except Exception:  # noqa: BLE001
        pass


async def _safe_clear_status(user_id: str, conversation_id: str) -> None:
    try:
        await realtime.clear_status(user_id, conversation_id)
    except Exception:  # noqa: BLE001
        pass
