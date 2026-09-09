"""add notifications table

Revision ID: a3b5f7c9e1d2
Revises: c9d7fa2fb7cf
Create Date: 2026-08-28 11:40:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b5f7c9e1d2"
down_revision: str | None = "c9d7fa2fb7cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("read", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_notifications_agent"), ["agent"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_notifications_level"), ["level"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_notifications_read"), ["read"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notifications_agent"))
        batch_op.drop_index(batch_op.f("ix_notifications_level"))
        batch_op.drop_index(batch_op.f("ix_notifications_read"))
    op.drop_table("notifications")