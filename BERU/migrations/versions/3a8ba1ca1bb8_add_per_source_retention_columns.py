"""add per-source retention columns

Revision ID: 3a8ba1ca1bb8
Revises: c1d2e3f4a5b6
Create Date: 2026-09-03 15:30:10.277254

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a8ba1ca1bb8"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add per-source audit retention policy columns (NULL = use global default).
    with op.batch_alter_table("scheduled_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("retention_days", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("keep_last", sa.Integer(), nullable=True))

    with op.batch_alter_table("monitor_triggers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("retention_days", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("keep_last", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("monitor_triggers", schema=None) as batch_op:
        batch_op.drop_column("keep_last")
        batch_op.drop_column("retention_days")

    with op.batch_alter_table("scheduled_tasks", schema=None) as batch_op:
        batch_op.drop_column("keep_last")
        batch_op.drop_column("retention_days")
