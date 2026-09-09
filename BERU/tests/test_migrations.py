"""Verify the Alembic migration is a faithful, runnable substitute for the old
``create_all`` bootstrap.

These tests require Alembic (a runtime dependency); if it is not installed the
whole module is skipped rather than failing collection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

pytest.importorskip("alembic")

from alembic import command  # noqa: E402  (after importorskip guard)

from backend.database.migrations import (  # noqa: E402
    make_alembic_config,
    run_migrations,
    to_sync_url,
)


def test_to_sync_url_strips_async_driver():
    assert to_sync_url("sqlite+aiosqlite:///./beru.db") == "sqlite:///./beru.db"
    assert to_sync_url("sqlite+aiosqlite:///:memory:") == "sqlite:///:memory:"
    # postgres async driver maps to a sync one
    assert to_sync_url("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    # Already-sync / unknown URLs pass through unchanged.
    assert to_sync_url("sqlite:///./beru.db") == "sqlite:///./beru.db"


def test_migration_upgrade_creates_expected_schema(tmp_path: Path):
    """`alembic upgrade head` against a fresh DB creates exactly the ORM schema."""
    db_file = tmp_path / "migrated.db"
    url = f"sqlite:///{db_file}"

    command.upgrade(make_alembic_config(url), "head")

    engine = sa.create_engine(url)
    try:
        inspector = sa.inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "conversations",
            "messages",
            "facts",
            "projects",
            "embeddings",
            "notifications",
            "scheduled_tasks",
            "monitor_triggers",
            "task_runs",
            "trigger_fires",
            "activity_records",
            "audit_records",
            "alembic_version",
        }.issubset(tables)
        # Stage 5.1 adds the accounts tables.
        assert {"users", "sessions"}.issubset(tables)

        conv_cols = {c["name"] for c in inspector.get_columns("conversations")}
        assert conv_cols == {
            "id",
            "title",
            "agent",
            "project_id",
            "user_id",
            "created_at",
            "updated_at",
        }

        # Stage 5.1 accounts tables + per-user uniqueness on facts and projects.
        users_cols = {c["name"] for c in inspector.get_columns("users")}
        assert users_cols == {
            "id",
            "username",
            "password_hash",
            "is_owner",
            "created_at",
            "updated_at",
        }
        index_names = {ix["name"] for ix in inspector.get_indexes("users")}
        assert "ix_users_username" in index_names

        sessions_cols = {c["name"] for c in inspector.get_columns("sessions")}
        assert sessions_cols == {
            "id",
            "token_hash",
            "api_key_hash",
            "user_id",
            "expires_at",
            "revoked_at",
            "created_at",
            "updated_at",
        }

        facts_cols = {c["name"] for c in inspector.get_columns("facts")}
        assert "user_id" in facts_cols
        facts_constraints = {
            c["name"]
            for c in inspector.get_unique_constraints("facts")
            if c["name"]
        }
        assert "uq_facts_user_key" in facts_constraints
        # The global unique key index is replaced by the per-user pair.
        facts_indexes = {ix["name"] for ix in inspector.get_indexes("facts")}
        assert "ix_facts_key" not in facts_indexes

        projects_cols = {c["name"] for c in inspector.get_columns("projects")}
        assert "user_id" in projects_cols
        projects_constraints = {
            c["name"]
            for c in inspector.get_unique_constraints("projects")
            if c["name"]
        }
        assert "uq_projects_user_name" in projects_constraints

        msg_cols = {c["name"] for c in inspector.get_columns("messages")}
        assert msg_cols == {
            "id",
            "conversation_id",
            "role",
            "content",
            "model",
            "token_count",
            "created_at",
            "updated_at",
        }

        # The conversation_id index is created with the same name the ORM uses.
        index_names = {ix["name"] for ix in inspector.get_indexes("messages")}
        assert "ix_messages_conversation_id" in index_names

        # The FK to conversations exists.
        fks = inspector.get_foreign_keys("messages")
        assert any(fk["referred_table"] == "conversations" for fk in fks)

        # Proactive persistence tables carry the full engine state.
        task_cols = {c["name"] for c in inspector.get_columns("scheduled_tasks")}
        assert task_cols == {
            "id",
            "created_at",
            "updated_at",
            "name",
            "description",
            "handler",
            "task_type",
            "status",
            "interval_seconds",
            "run_at",
            "cron_expr",
            "last_run",
            "next_run",
            "run_count",
            "max_runs",
            "last_error",
            "payload",
            "agent",
            "retention_days",
            "keep_last",
        }
        trigger_cols = {c["name"] for c in inspector.get_columns("monitor_triggers")}
        assert trigger_cols == {
            "id",
            "created_at",
            "updated_at",
            "name",
            "description",
            "status",
            "condition",
            "actions",
            "last_fired",
            "fire_count",
            "cooldown_seconds",
            "retention_days",
            "keep_last",
        }

        # Audit-history tables mirror the engine hooks' append-only logs.
        runs_cols = {c["name"] for c in inspector.get_columns("task_runs")}
        assert runs_cols == {
            "id",
            "created_at",
            "updated_at",
            "task_id",
            "run_at",
            "status",
            "duration_ms",
            "run_count",
            "error",
            "seq",
        }
        index_names = {ix["name"] for ix in inspector.get_indexes("task_runs")}
        assert "ix_task_runs_task_id" in index_names
        assert "ix_task_runs_run_at" in index_names
        assert "ix_task_runs_seq" in index_names

        fires_cols = {c["name"] for c in inspector.get_columns("trigger_fires")}
        assert fires_cols == {
            "id",
            "created_at",
            "updated_at",
            "trigger_id",
            "fired_at",
            "condition",
            "value",
            "seq",
        }
        index_names = {ix["name"] for ix in inspector.get_indexes("trigger_fires")}
        assert "ix_trigger_fires_trigger_id" in index_names
        assert "ix_trigger_fires_fired_at" in index_names
        assert "ix_trigger_fires_seq" in index_names

        # Durable reliability ledger tables mirror the activity/audit entries.
        activity_cols = {c["name"] for c in inspector.get_columns("activity_records")}
        assert activity_cols == {
            "id",
            "created_at",
            "updated_at",
            "seq",
            "timestamp",
            "request_id",
            "agent",
            "tool_name",
            "args_summary",
            "outcome",
            "duration_ms",
            "error",
        }
        index_names = {ix["name"] for ix in inspector.get_indexes("activity_records")}
        assert "ix_activity_records_seq" in index_names
        assert "ix_activity_records_timestamp" in index_names
        assert "ix_activity_records_outcome" in index_names

        audit_cols = {c["name"] for c in inspector.get_columns("audit_records")}
        assert audit_cols == {
            "id",
            "created_at",
            "updated_at",
            "seq",
            "timestamp",
            "request_id",
            "agent",
            "tool_name",
            "decision",
            "confirmation_id",
            "outcome",
            "error",
        }
        index_names = {ix["name"] for ix in inspector.get_indexes("audit_records")}
        assert "ix_audit_records_seq" in index_names
        assert "ix_audit_records_timestamp" in index_names
        assert "ix_audit_records_decision" in index_names
    finally:
        engine.dispose()


def test_migration_downgrade_removes_tables(tmp_path: Path):
    """`downgrade base` cleanly drops the schema (round-trip is reversible)."""
    db_file = tmp_path / "roundtrip.db"
    url = f"sqlite:///{db_file}"
    cfg = make_alembic_config(url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = sa.create_engine(url)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert "conversations" not in tables
        assert "messages" not in tables
        assert "facts" not in tables
        assert "projects" not in tables
        assert "embeddings" not in tables
        assert "notifications" not in tables
        assert "scheduled_tasks" not in tables
        assert "monitor_triggers" not in tables
        assert "task_runs" not in tables
        assert "trigger_fires" not in tables
        assert "activity_records" not in tables
        assert "audit_records" not in tables
        assert "users" not in tables
        assert "sessions" not in tables
    finally:
        engine.dispose()


def test_legacy_unversioned_db_partial_schema_is_refused(tmp_path):
    """A pre-migration database missing head tables is refused, not blindly
    stamped (stamping would silently skip every later schema change)."""
    url = f"sqlite:///{tmp_path / 'legacy-partial.db'}"
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE conversations ("
                    "id VARCHAR(36) PRIMARY KEY, title VARCHAR(255))"
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="Refusing to auto-adopt"):
        run_migrations(url)


def test_legacy_unversioned_db_full_schema_is_adopted(tmp_path):
    """A complete pre-migration schema (all current tables, no Alembic version)
    is adopted by stamping to head."""
    import backend.models  # noqa: F401  (register models on Base.metadata)
    from backend.database.base import Base

    url = f"sqlite:///{tmp_path / 'legacy-full.db'}"
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    run_migrations(url)

    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            version = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            inspected = sa.inspect(conn).get_table_names()
    finally:
        engine.dispose()
    assert "conversations" in inspected
    # The stamped revision must exactly match the latest migration head.
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(make_alembic_config(url)).get_heads()
    assert version == heads[0]
