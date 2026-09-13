"""附件表 files。

Revision ID: 0002_files
Revises: 0001_initial
Create Date: 2026-01-02

附件归属校验依赖本表（user_id + file_id），而不是对象路径前缀。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_files"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=32),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=False, server_default="application/octet-stream"),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bucket", sa.String(length=128), nullable=False),
        sa.Column("object_path", sa.String(length=768), nullable=False),
        sa.Column("storage_uri", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_files_user_id", "files", ["user_id"])
    op.create_index("ix_files_conversation_id", "files", ["conversation_id"])
    op.create_index("ix_files_user_created", "files", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("files")
