"""SQLite robustness: connection pragmas and integrity constraints.

Covers the release-hardening SQLite work:
* ``_set_sqlite_pragmas`` is installed on app engines (FKs ON, busy timeout,
  journal mode) and actually changes SQLite behaviour — the FK test proves
  cascade deletes fire only when the pragma is applied (SQLite defaults to
  ``foreign_keys=0`` per connection).
* The embeddings table has a composite unique key on ``(source_table,
  source_id)`` and the vector store upserts through it atomically.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database.base import Base, _set_sqlite_pragmas
from backend.memory.vector_store import EmbeddingRow, VectorStore


async def test_set_sqlite_pragmas_applies_hardening():
    """The connect hook sets the documented pragmas on a live connection."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    _set_sqlite_pragmas(conn, None)

    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1
    cursor.execute("PRAGMA busy_timeout")
    assert cursor.fetchone()[0] == 5000
    cursor.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0] in ("memory", "wal")
    conn.close()


async def test_engine_wiring_sets_pragmas_on_live_connection(tmp_path):
    """The app's engine wiring (engine + ``connect`` event hook, exactly as
    ``backend.database.base.get_engine`` builds it) reports all three hardening
    pragmas on a real connection opened by that engine."""
    engine = _fk_engine(tmp_path / "wired.db")
    async with engine.connect() as conn:
        fk = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
        timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
        journal_mode = (
            await conn.exec_driver_sql("PRAGMA journal_mode")
        ).scalar_one()
    await engine.dispose()
    assert fk == 1
    assert timeout == 5000
    assert journal_mode == "wal"


def _fk_engine(db_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    return engine


async def _seed_project_conversations(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        from backend.models.project import Project

        project = Project(name="scoped")
        session.add(project)
        await session.flush()
        session.add(
            __import__("backend.models", fromlist=["Conversation"]).Conversation(
                agent="beru_core", title="t", project_id=project.id
            )
        )
        await session.commit()
        return project.id


async def test_foreign_keys_pragma_enables_cascade(tmp_path):
    """With the pragma wired in, deleting a project cascades its conversations."""
    import sqlalchemy as sa

    engine = _fk_engine(tmp_path / "fk.db")
    await _seed_project_conversations(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    from backend.models.project import Project

    async with maker() as session:
        await session.execute(sa.delete(Project))
        await session.commit()

    from backend.models.conversation import Conversation

    async with maker() as session:
        remaining = (
            await session.execute(sa.select(sa.func.count(Conversation.id)))
        ).scalar_one()
    assert remaining == 0  # FK ON -> ON DELETE CASCADE removed the children
    await engine.dispose()


async def test_without_pragma_fk_ignored_leaves_orphans(tmp_path):
    """SQLite default (foreign_keys off) leaves orphans — proves the pragma matters."""
    import sqlalchemy as sa

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'plain.db'}",
        connect_args={"check_same_thread": False},
    )  # NOTE: no event listener installs the pragma
    project_id = await _seed_project_conversations(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    from backend.models.project import Project

    async with maker() as session:
        await session.execute(sa.delete(Project))
        await session.commit()

    from backend.models.conversation import Conversation

    async with maker() as session:
        remaining = (
            await session.execute(sa.select(sa.func.count(Conversation.id)))
        ).scalar_one()
    assert remaining == 1  # orphan survives — FK enforcement was OFF
    assert project_id
    await engine.dispose()


async def test_embeddings_unique_and_atomic_upsert(_sessionmaker):
    """Upserting twice on one entity yields one row updated in place."""
    store = VectorStore()
    async with _sessionmaker() as session:
        await store.upsert(
            session, source_table="messages", source_id="m1", text="first",
            vector=[1.0, 0.0],
        )
        await session.commit()

    async with _sessionmaker() as session:
        await store.upsert(
            session, source_table="messages", source_id="m1", text="second",
            vector=[0.0, 1.0],
        )
        await session.commit()

    async with _sessionmaker() as session:
        hits = await store.search(session, [1.0, 0.0], source_table="messages", top_k=5)
        assert len(hits) == 1
        assert hits[0].source_id == "m1"
        assert hits[0].text == "second"

    async with _sessionmaker() as session:
        rows = (await session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(EmbeddingRow)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].dimension == 2


async def test_embeddings_composite_unique_rejects_duplicates(_sessionmaker):
    """Directly inserting a second row for one entity violates the constraint."""
    async with _sessionmaker() as session:
        session.add(EmbeddingRow(
            source_table="messages", source_id="m9", text="a",
            vector_json="[1.0]", dimension=1,
        ))
        await session.commit()

    async with _sessionmaker() as session:
        session.add(EmbeddingRow(
            source_table="messages", source_id="m9", text="b",
            vector_json="[2.0]", dimension=1,
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


async def _seed_task_with_runs(engine, task_id: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.engines.scheduler import ScheduledTask
    from backend.services.proactive_store import (
        record_task_run,
        save_scheduled_task,
    )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    task = ScheduledTask(id=task_id, name="t", run_count=0)
    async with maker() as session:
        await save_scheduled_task(session, task)
        await session.commit()
    for _ in range(3):
        async with maker() as session:
            await record_task_run(session, task_id=task_id, status="completed")
            await session.commit()


async def _seed_trigger_with_fires(engine, trigger_id: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.engines.monitor import Trigger
    from backend.services.proactive_store import (
        record_trigger_fire,
        save_monitor_trigger,
    )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    trigger = Trigger(id=trigger_id, name="tg")
    async with maker() as session:
        await save_monitor_trigger(session, trigger)
        await session.commit()
    for _ in range(2):
        async with maker() as session:
            await record_trigger_fire(session, trigger_id=trigger_id, value=1)
            await session.commit()


async def test_delete_task_cascades_to_task_runs_with_fk_pragma(tmp_path):
    """Deleting a task removes its run history (DB + ORM cascade), not just the
    parent row."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.models.task_run import TaskRunRecord
    from backend.services.proactive_store import delete_scheduled_task

    engine = _fk_engine(tmp_path / "task_cascade.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task_with_runs(engine, "task-1")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        removed = await delete_scheduled_task(session, "task-1")
        await session.commit()
    assert removed is True

    async with maker() as session:
        remaining_runs = await session.scalar(
            sa.select(sa.func.count(TaskRunRecord.id))
        )
    assert remaining_runs == 0  # FK ON -> ON DELETE CASCADE removed the children
    await engine.dispose()


async def test_delete_trigger_cascades_to_trigger_fires_with_fk_pragma(tmp_path):
    """Deleting a trigger removes its fire history (DB + ORM cascade)."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.models.trigger_fire import TriggerFireRecord
    from backend.services.proactive_store import delete_monitor_trigger

    engine = _fk_engine(tmp_path / "trigger_cascade.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_trigger_with_fires(engine, "trigger-1")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        removed = await delete_monitor_trigger(session, "trigger-1")
        await session.commit()
    assert removed is True

    async with maker() as session:
        remaining_fires = await session.scalar(
            sa.select(sa.func.count(TriggerFireRecord.id))
        )
    assert remaining_fires == 0
    await engine.dispose()


async def test_without_fk_pragma_task_delete_leaves_orphaned_runs(tmp_path):
    """SQLite default (foreign keys off) leaves orphaned runs — the pragma is
    what makes cascade deletion real."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.models.task_run import TaskRunRecord
    from backend.services.proactive_store import delete_scheduled_task

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'orphan_control.db'}",
        connect_args={"check_same_thread": False},
    )  # NOTE: no pragma listener -> foreign_keys stays OFF
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task_with_runs(engine, "task-1")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        await delete_scheduled_task(session, "task-1")
        await session.commit()

    async with maker() as session:
        orphaned_runs = await session.scalar(
            sa.select(sa.func.count(TaskRunRecord.id))
        )
    assert orphaned_runs == 3  # children survive when FK enforcement is OFF
    await engine.dispose()