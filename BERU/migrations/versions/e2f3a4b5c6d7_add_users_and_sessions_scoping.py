"""add users and sessions tables and per-owner scoping columns

Revision ID: e2f3a4b5c6d7
Revises: b5c6d7e8f9a1
Create Date: 2026-09-09 09:00:00.000000

Stage 5.1 (multi-user isolation):

* ``users`` — one row per person; the ``is_owner`` account is the superuser.
* ``sessions`` — the durable DB-backed store of login sessions (only SHA-256
  token hashes + the issuing API-key realm hash are persisted, never the raw
  token or key).
* ``conversations.user_id`` / ``facts.user_id`` / ``projects.user_id`` —
  nullable owner columns (NULL keeps the legacy single-user behaviour: the row
  belongs to the system/owner and is visible to any owner-scoped query).
* The global unique index on ``facts.key`` is replaced by a ``(user_id, key)``
  unique index so different users can remember the same key independently.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "b5c6d7e8f9a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=True),
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_username"), ["username"], unique=True)
        batch_op.create_index(batch_op.f("ix_users_is_owner"), ["is_owner"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sessions_token_hash"), ["token_hash"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_sessions_api_key_hash"), ["api_key_hash"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_sessions_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_sessions_revoked_at"), ["revoked_at"], unique=False)

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f("ix_conversations_user_id"), ["user_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_conversations_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )

    # Facts lose the global unique key index in favour of a per-user one.
    with op.batch_alter_table("facts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_facts_key"))
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f("ix_facts_user_id"), ["user_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_facts_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.create_unique_constraint(
            "uq_facts_user_key", ["user_id", "key"]
        )

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f("ix_projects_user_id"), ["user_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_projects_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        # Project names become unique per user instead of globally unique.
        batch_op.drop_index(batch_op.f("ix_projects_name"))
        batch_op.create_index("ix_projects_name", ["name"], unique=False)
        batch_op.create_unique_constraint(
            "uq_projects_user_name", ["user_id", "name"]
        )


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint("uq_projects_user_name", type_="unique")
        batch_op.drop_constraint("fk_projects_user_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_projects_user_id"))
        batch_op.drop_column("user_id")
        # Restore the global unique name index from the original schema.
        batch_op.drop_index("ix_projects_name")
        batch_op.create_index(batch_op.f("ix_projects_name"), ["name"], unique=True)

    with op.batch_alter_table("facts", schema=None) as batch_op:
        batch_op.drop_constraint("uq_facts_user_key", type_="unique")
        batch_op.drop_constraint("fk_facts_user_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_facts_user_id"))
        batch_op.drop_column("user_id")
        # Restore the global unique key index from the original schema.
        batch_op.create_index(batch_op.f("ix_facts_key"), ["key"], unique=True)

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_constraint("fk_conversations_user_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_conversations_user_id"))
        batch_op.drop_column("user_id")

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sessions_revoked_at"))
        batch_op.drop_index(batch_op.f("ix_sessions_user_id"))
        batch_op.drop_index(batch_op.f("ix_sessions_api_key_hash"))
        batch_op.drop_index(batch_op.f("ix_sessions_token_hash"))

    op.drop_table("sessions")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_is_owner"))
        batch_op.drop_index(batch_op.f("ix_users_username"))

    op.drop_table("users")