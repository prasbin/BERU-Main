"""add seq >= 0 check constraints to reliability ledger tables

Revision ID: 1a2b3c4d5e6f
Revises: 9e7f8a9b0c1d
Create Date: 2026-09-08 10:30:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1a2b3c4d5e6f"
down_revision: str | None = "9e7f8a9b0c1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("activity_records", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_activity_records_seq_nonneg", "seq >= 0"
        )
    with op.batch_alter_table("audit_records", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_audit_records_seq_nonneg", "seq >= 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_records", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_audit_records_seq_nonneg", type_="check"
        )
    with op.batch_alter_table("activity_records", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_activity_records_seq_nonneg", type_="check"
        )