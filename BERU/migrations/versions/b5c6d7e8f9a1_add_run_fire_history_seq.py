"""add monotonic seq columns to run/fire history tables

Revision ID: b5c6d7e8f9a1
Revises: 1a2b3c4d5e6f
Create Date: 2026-09-08 12:00:00.000000

Adds a ``seq`` insert-order counter to ``task_runs`` and ``trigger_fires`` so
"newest first" ordering is deterministic even when rows share the same
``run_at``/``fired_at`` timestamp (possible at microsecond precision) — the
previous tie-breakers (``created_at`` at second precision and a random UUID
row ``id``) left same-instant rows ordered nondeterministically.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5c6d7e8f9a1"
down_revision: str | None = "1a2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_check_constraint(
            "ck_task_runs_seq_nonneg", "seq >= 0"
        )
        batch_op.create_index("ix_task_runs_seq", ["seq"])
    # Backfill legacy rows in insertion order (SQLite rowid is monotonic).
    op.execute("UPDATE task_runs SET seq = rowid")
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.alter_column("seq", server_default=None)

    with op.batch_alter_table("trigger_fires", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_check_constraint(
            "ck_trigger_fires_seq_nonneg", "seq >= 0"
        )
        batch_op.create_index("ix_trigger_fires_seq", ["seq"])
    op.execute("UPDATE trigger_fires SET seq = rowid")
    with op.batch_alter_table("trigger_fires", schema=None) as batch_op:
        batch_op.alter_column("seq", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("trigger_fires", schema=None) as batch_op:
        batch_op.drop_constraint("ck_trigger_fires_seq_nonneg", type_="check")
        batch_op.drop_index("ix_trigger_fires_seq")
        batch_op.drop_column("seq")
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_task_runs_seq_nonneg", type_="check")
        batch_op.drop_index("ix_task_runs_seq")
        batch_op.drop_column("seq")