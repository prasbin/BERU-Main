"""add projects table and scoping fields

Revision ID: c9d7fa2fb7cf
Revises: 221d00777edb
Create Date: 2026-08-26 23:55:11.827027

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d7fa2fb7cf"
down_revision: str | None = "221d00777edb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_projects_name"), ["name"], unique=True)

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_conversations_project_id"), ["project_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_conversations_project_id",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("facts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f("ix_facts_agent"), ["agent"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_facts_project_id"), ["project_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_facts_project_id",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("facts", schema=None) as batch_op:
        batch_op.drop_constraint("fk_facts_project_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_facts_project_id"))
        batch_op.drop_index(batch_op.f("ix_facts_agent"))
        batch_op.drop_column("project_id")
        batch_op.drop_column("agent")

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_constraint("fk_conversations_project_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_conversations_project_id"))
        batch_op.drop_column("project_id")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projects_name"))

    op.drop_table("projects")
