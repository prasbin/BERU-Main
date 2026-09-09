"""add activity_records and audit_records tables (durable reliability ledger)

Revision ID: d4e5f6a7b8c9
Revises: 3a8ba1ca1bb8
Create Date: 2026-09-07 12:00:00.000000

The in-memory activity ledger and approval audit trail get durable tables so
observability data survives process restarts (mirrors the proactive run/fire
audit tables).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "3a8ba1ca1bb8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "activity_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("args_summary", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("activity_records", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_activity_records_seq"), ["seq"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_activity_records_timestamp"), ["timestamp"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_activity_records_outcome"), ["outcome"], unique=False
        )

    op.create_table(
        "audit_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("confirmation_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_records", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_audit_records_seq"), ["seq"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_audit_records_timestamp"), ["timestamp"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_audit_records_decision"), ["decision"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_records_decision"))
        batch_op.drop_index(batch_op.f("ix_audit_records_timestamp"))
        batch_op.drop_index(batch_op.f("ix_audit_records_seq"))
    op.drop_table("audit_records")
    with op.batch_alter_table("activity_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_activity_records_outcome"))
        batch_op.drop_index(batch_op.f("ix_activity_records_timestamp"))
        batch_op.drop_index(batch_op.f("ix_activity_records_seq"))
    op.drop_table("activity_records")
