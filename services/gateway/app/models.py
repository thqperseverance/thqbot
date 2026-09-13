"""ORM 模型：用户、会话、消息。

设计要点（对应 D5=A / D6=A / D7=A）：
- 会话与消息以 PostgreSQL 为唯一事实来源；Redis 只做缓存、去重与推送通道。
- ``Message.seq`` 是单调自增主键，用于稳定排序与游标分页（跨 PG/SQLite 可用）。
- ``Message.external_id`` 承载 Kafka 事件 id，唯一约束天然提供消费幂等。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .db import Base, TimestampMixin

# PG 用 JSONB，其它方言（测试用 SQLite）退化为 JSON
JSONType = JSON().with_variant(JSONB, "postgresql")

SENDER_USER = "user"
SENDER_BOT = "bot"


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<User {self.username}>"


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    bot_id: Mapped[str] = mapped_column(String(64), default="bot_A", nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="新会话", nullable=False)
    unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_conversations_user_last_message", "user_id", "last_message_at"),
    )


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    # 单调递增游标；PG 渲染为 SERIAL，SQLite 渲染为 INTEGER PRIMARY KEY
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    sender: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="sent", nullable=False)
    request_msg_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    reply_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Kafka 事件 id：唯一约束保证重复投递只落一条（消费幂等）
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )


class FileRecord(Base, TimestampMixin):
    """已上传到对象存储的文件元数据。

    归属校验必须基于本表（而不是"路径前缀"这种弱校验）：
    下载时先按 ``user_id + file_id`` 找到记录，再用记录里的 ``object_path`` 去取对象。
    """

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="SET NULL"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), default="application/octet-stream", nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_path: Mapped[str] = mapped_column(String(768), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)

    __table_args__ = (
        Index("ix_files_user_created", "user_id", "created_at"),
    )
