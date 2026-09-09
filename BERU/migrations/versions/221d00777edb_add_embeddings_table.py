"""add embeddings table

Revision ID: 221d00777edb
Revises: f8a115915733
Create Date: 2026-08-26 23:47:04.373239

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "221d00777edb"
down_revision: str | None = "f8a115915733"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("vector_json", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
    )
    with op.batch_alter_table("embeddings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_embeddings_source_table"), ["source_table"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_embeddings_source_id"), ["source_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("embeddings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_embeddings_source_id"))
        batch_op.drop_index(batch_op.f("ix_embeddings_source_table"))
    op.drop_table("embeddings")
