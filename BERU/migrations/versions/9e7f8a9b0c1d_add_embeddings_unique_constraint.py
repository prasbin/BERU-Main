"""add composite unique constraint on embeddings (source_table, source_id)

Revision ID: 9e7f8a9b0c1d
Revises: d4e5f6a7b8c9
Create Date: 2026-09-07 20:00:00.000000

Enforces one row per source entity so the vector store can upsert atomically
via ON CONFLICT DO UPDATE. For a fresh install no cleanup is needed; existing
installations with duplicate rows (none expected from earlier upsert logic)
are deduplicated before the unique index is created.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e7f8a9b0c1d"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE_NAME = "uq_embeddings_source_entity"
#: Column text uses a row_number partition so all but one copy per entity is kept.
_DEDUP = sa.text("""DELETE FROM embeddings WHERE id NOT IN (
    SELECT id FROM (
        SELECT id,
               ROW_NUMBER() OVER (PARTITION BY source_table, source_id ORDER BY id) AS rn
        FROM embeddings
    ) WHERE rn = 1
)""")


def upgrade() -> None:
    # Remove any pre-existing duplicates (older upsert wrote via ORM, one per key).
    op.execute(_DEDUP)
    with op.batch_alter_table("embeddings", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            _UNIQUE_NAME, ["source_table", "source_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("embeddings", schema=None) as batch_op:
        batch_op.drop_constraint(_UNIQUE_NAME, type_="unique")
