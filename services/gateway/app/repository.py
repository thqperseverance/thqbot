"""PostgreSQL 数据访问层（会话 / 消息 / 用户）。

这是 D5=A 的落地：会话与消息以 PG 为唯一事实来源。
所有读写都带 ``user_id`` 归属校验，越权访问直接抛 :class:`NotFound`（对应 D17=A：
下载/读取路径不允许"只按 id 前缀"这种弱校验）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .models import Conversation, Message, User

DEFAULT_CONVERSATION_TITLE = "新会话"
MAX_TITLE_LENGTH = 60


class NotFound(Exception):
    """资源不存在或不属于当前用户。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# --------------------------------------------------------------------------- 用户


def get_user_by_username(session: Session, username: str) -> User | None:
    if not username:
        return None
    return session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()


def get_user(session: Session, user_id: str) -> User | None:
    if not user_id:
        return None
    return session.get(User, user_id)


def create_user(
    session: Session,
    *,
    username: str,
    password_hash: str,
    display_name: str = "",
) -> User:
    user = User(username=username, password_hash=password_hash, display_name=display_name or username)
    session.add(user)
    session.flush()
    return user


def count_users(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(User)).scalar_one())


# --------------------------------------------------------------------------- 会话


def list_conversations(session: Session, user_id: str, *, bot_id: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Conversation).where(Conversation.user_id == user_id)
    if bot_id:
        stmt = stmt.where(Conversation.bot_id == bot_id)
    stmt = stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
    conversations = list(session.execute(stmt).scalars().all())
    if not conversations:
        return []

    ids = [c.id for c in conversations]
    latest = (
        select(Message.conversation_id, func.max(Message.seq).label("max_seq"))
        .where(Message.conversation_id.in_(ids))
        .group_by(Message.conversation_id)
        .subquery()
    )
    latest_rows = session.execute(
        select(Message).join(latest, Message.seq == latest.c.max_seq)
    ).scalars().all()
    preview_map: dict[str, Message] = {row.conversation_id: row for row in latest_rows}

    out: list[dict[str, Any]] = []
    for conversation in conversations:
        last = preview_map.get(conversation.id)
        out.append(
            {
                "conversation_id": conversation.id,
                "title": conversation.title,
                "bot_id": conversation.bot_id,
                "unread_count": conversation.unread_count,
                "created_at": _iso(conversation.created_at),
                "updated_at": _iso(conversation.updated_at),
                "last_message_at": _iso(conversation.last_message_at),
                "last_message_preview": _preview(last.content if last else ""),
            }
        )
    return out


def _preview(text: str, limit: int = 80) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed[:limit]


def create_conversation(
    session: Session,
    *,
    user_id: str,
    bot_id: str,
    title: str | None = None,
) -> Conversation:
    conversation = Conversation(
        user_id=user_id,
        bot_id=bot_id,
        title=(title or DEFAULT_CONVERSATION_TITLE)[:200],
        unread_count=0,
    )
    session.add(conversation)
    session.flush()
    return conversation


def get_or_create_default_conversation(session: Session, *, user_id: str, bot_id: str) -> Conversation:
    """演示版：每个用户在每个 bot 下有一个默认会话。"""
    existing = session.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.bot_id == bot_id)
        .order_by(Conversation.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return create_conversation(session, user_id=user_id, bot_id=bot_id)


def get_owned_conversation(session: Session, *, user_id: str, conversation_id: str) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user_id:
        # 不区分"不存在"与"不属于你"，避免探测
        raise NotFound(conversation_id)
    return conversation


def rename_conversation_if_default(session: Session, conversation: Conversation, content: str) -> None:
    if conversation.title not in (DEFAULT_CONVERSATION_TITLE, "", None):
        return
    title = " ".join((content or "").split())[:MAX_TITLE_LENGTH]
    if title:
        conversation.title = title


# --------------------------------------------------------------------------- 消息


def _to_out(message: Message) -> dict[str, Any]:
    return {
        "message_id": message.id,
        "conversation_id": message.conversation_id,
        "sender": message.sender,
        "content": message.content,
        "content_type": message.content_type,
        "status": message.status,
        "request_msg_id": message.request_msg_id,
        "reply_to": message.reply_to,
        "created_at": _iso(message.created_at),
        "meta": message.meta or {},
    }


def append_message(
    session: Session,
    *,
    conversation: Conversation,
    sender: str,
    content: str,
    content_type: str = "text",
    status: str = "sent",
    request_msg_id: str | None = None,
    reply_to: str | None = None,
    external_id: str | None = None,
    meta: dict[str, Any] | None = None,
    increment_unread: bool = False,
) -> Message | None:
    """写入一条消息。

    当 ``external_id`` 已存在时返回 ``None``（Kafka 重复投递，幂等丢弃）。
    """
    message = Message(
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        sender=sender,
        content=content or "",
        content_type=content_type,
        status=status,
        request_msg_id=request_msg_id,
        reply_to=reply_to,
        external_id=external_id,
        meta=meta or {},
    )
    session.add(message)
    try:
        session.flush()
    except IntegrityError:
        # 唯一约束命中：已存在同 external_id 的消息，回滚本次插入即可
        session.rollback()
        return None

    conversation.last_message_at = message.created_at or _utcnow()
    if increment_unread:
        conversation.unread_count = int(conversation.unread_count or 0) + 1
    session.flush()
    return message


def find_by_external_id(session: Session, external_id: str) -> Message | None:
    if not external_id:
        return None
    return session.execute(
        select(Message).where(Message.external_id == external_id)
    ).scalar_one_or_none()


def find_user_message_by_request_id(
    session: Session, *, conversation_id: str, request_msg_id: str
) -> Message | None:
    """按 request_msg_id 找该轮的用户消息（用于计算端到端耗时）。"""
    if not request_msg_id:
        return None
    return session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.request_msg_id == request_msg_id,
            Message.sender == "user",
        )
        .order_by(Message.seq.asc())
        .limit(1)
    ).scalar_one_or_none()


def list_messages(
    session: Session,
    *,
    conversation_id: str,
    limit: int = 40,
    before_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按 ``seq`` 倒序取一页，返回时反转为升序（便于前端直接追加）。"""
    limit = max(1, min(int(limit), 200))
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if before_id:
        anchor = session.execute(
            select(Message.seq).where(Message.id == before_id, Message.conversation_id == conversation_id)
        ).scalar_one_or_none()
        if anchor is not None:
            stmt = stmt.where(Message.seq < anchor)
    stmt = stmt.order_by(Message.seq.desc()).limit(limit + 1)
    rows = list(session.execute(stmt).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    paging = {
        "limit": limit,
        "has_more": has_more,
        "oldest_message_id": rows[0].id if rows else None,
        "newest_message_id": rows[-1].id if rows else None,
    }
    return [_to_out(row) for row in rows], paging


def list_messages_after_seq(session: Session, *, conversation_id: str, after_seq: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = list(
        session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.seq > after_seq)
            .order_by(Message.seq.asc())
            .limit(limit)
        ).scalars().all()
    )
    return [_to_out(row) for row in rows]


def mark_read(session: Session, conversation: Conversation) -> dict[str, Any]:
    conversation.unread_count = 0
    session.flush()
    return {
        "conversation_id": conversation.id,
        "unread_count": 0,
        "title": conversation.title,
        "bot_id": conversation.bot_id,
    }


def delete_messages(session: Session, *, conversation_id: str, message_ids: Sequence[str]) -> int:
    ids = [mid for mid in message_ids if mid]
    if not ids:
        return 0
    rows = list(
        session.execute(
            select(Message).where(Message.conversation_id == conversation_id, Message.id.in_(ids))
        ).scalars().all()
    )
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def total_unread(session: Session, user_id: str) -> int:
    value = session.execute(
        select(func.coalesce(func.sum(Conversation.unread_count), 0)).where(Conversation.user_id == user_id)
    ).scalar_one()
    return int(value or 0)


def serialize_message(message: Message) -> dict[str, Any]:
    return _to_out(message)


def merge_meta(existing: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """合并 meta（浅合并，extra 覆盖同名键）。"""
    merged: dict[str, Any] = dict(existing or {})
    if extra:
        merged.update(extra)
    return json.loads(json.dumps(merged, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- 文件


def create_file_record(
    session: Session,
    *,
    user_id: str,
    conversation_id: str | None,
    name: str,
    mime: str,
    size: int,
    bucket: str,
    object_path: str,
    storage_uri: str,
    file_id: str | None = None,
) -> models.FileRecord:
    fields: dict[str, Any] = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "name": name,
        "mime": mime,
        "size": int(size or 0),
        "bucket": bucket,
        "object_path": object_path,
        "storage_uri": storage_uri,
    }
    if file_id:
        fields["id"] = file_id
    record = models.FileRecord(**fields)
    session.add(record)
    session.flush()
    return record


def get_owned_file(session: Session, *, user_id: str, file_id: str) -> models.FileRecord:
    """按 ``user_id + file_id`` 取记录；不存在或不属于该用户都抛 NotFound。

    这是附件归属校验的唯一入口 —— 不做"路径前缀"这类弱校验。
    """
    record = session.get(models.FileRecord, file_id)
    if record is None or record.user_id != user_id:
        raise NotFound(file_id)
    return record


def list_owned_files(session: Session, *, user_id: str, file_ids: Sequence[str]) -> list[models.FileRecord]:
    """按给定顺序取回文件记录；任一不存在或不属于该用户即抛 NotFound。"""
    ordered: list[models.FileRecord] = []
    for file_id in file_ids:
        if not file_id:
            continue
        ordered.append(get_owned_file(session, user_id=user_id, file_id=file_id))
    return ordered


def list_conversation_files(
    session: Session, *, user_id: str, conversation_id: str, limit: int = 50
) -> list[models.FileRecord]:
    rows = session.execute(
        select(models.FileRecord)
        .where(
            models.FileRecord.user_id == user_id,
            models.FileRecord.conversation_id == conversation_id,
        )
        .order_by(models.FileRecord.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    ).scalars().all()
    return list(rows)


def delete_file_record(session: Session, record: models.FileRecord) -> None:
    session.delete(record)
    session.flush()


def serialize_file(record: models.FileRecord) -> dict[str, Any]:
    return {
        "file_id": record.id,
        "name": record.name,
        "mime": record.mime,
        "size": record.size,
        "storage_uri": record.storage_uri,
        "download_url": f"/api/files/{record.id}",
        "created_at": _iso(record.created_at),
    }


def build_attachment_payload(record: models.FileRecord) -> dict[str, Any]:
    """构造发给 ithqbot 的附件描述（与 SKILL_STANDARDS 的 files[] 语义一致）。

    ``storage`` / ``storage_uri`` 两者都带：ithqbot 的 ``_normalize_attachments``
    优先读 ``storage``，而技能（如 ``doc_compare``）会从中取出
    ``minio://bucket/path`` 直接去对象存储读取。
    """
    storage = {
        "backend": "minio",
        "bucket": record.bucket,
        "path": record.object_path,
        "storage_uri": record.storage_uri,
    }
    return {
        "kind": "file",
        "file_id": record.id,
        "name": record.name,
        "original_file_name": record.name,
        "mime": record.mime,
        "size": record.size,
        "rel_path": record.object_path,
        "storage_uri": record.storage_uri,
        "minio_uri": record.storage_uri,
        "storage_backend": "minio",
        "storage_bucket": record.bucket,
        "storage": storage,
        "download_url": f"/api/files/{record.id}",
    }
