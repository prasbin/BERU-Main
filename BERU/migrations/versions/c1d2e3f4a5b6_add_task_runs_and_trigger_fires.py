"""add task_runs and trigger_fires tables (run/fire audit history)

Revision ID: c1d2e3f4a5b6
Revises: b1e2f3a4c5d6
Create Date: 2026-08-28 16:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b1e2f3a4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["scheduled_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_task_runs_task_id"), ["task_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_task_runs_run_at"), ["run_at"], unique=False)

    op.create_table(
        "trigger_fires",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger_id", sa.String(length=36), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("condition", sa.JSON(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["trigger_id"], ["monitor_triggers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("trigger_fires", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_trigger_fires_trigger_id"), ["trigger_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_trigger_fires_fired_at"), ["fired_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("trigger_fires", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trigger_fires_fired_at"))
        batch_op.drop_index(batch_op.f("ix_trigger_fires_trigger_id"))
    op.drop_table("trigger_fires")
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_runs_run_at"))
        batch_op.drop_index(batch_op.f("ix_task_runs_task_id"))
    op.drop_table("task_runs")