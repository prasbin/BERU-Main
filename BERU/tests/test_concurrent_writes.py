"""Concurrent writers against one SQLite file never lose data.

WAL journaling + ``busy_timeout`` make short concurrent write transactions
serialise instead of failing, so the scheduler/monitor background persistence,
the reliability ledger, and hot-path upserts (facts, embeddings) must all keep
every row — no silent drops, no leaked orphans, no duplicate rows.

Each helper opens its own short-lived session (as the runtime background loops
do); ``asyncio.gather`` drives them truly concurrently against a shared
pragma-wired engine.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database.base import Base, _set_sqlite_pragmas
from backend.engines.scheduler import ScheduledTask
from backend.services.activity_ledger import (
    ActivityEntry,
    AuditEntry,
    get_activity_ledger,
    reset_activity_ledger,
    set_ledger_session_factory,
)


def _concurrent_engine(db_path):
    """An engine with the same pragma hardening the app engine gets."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    return engine


async def _schema(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _one_run(maker, task_id: str):
    from backend.services.proactive_store import record_task_run

    async with maker() as session:
        await record_task_run(session, task_id=task_id, status="completed")
        await session.commit()


async def _one_fire(maker, trigger_id: str):
    from backend.services.proactive_store import record_trigger_fire

    async with maker() as session:
        await record_trigger_fire(session, trigger_id=trigger_id, value=42)
        await session.commit()


async def _one_fact(maker, key: str, value: str):
    from backend.services.fact_service import FactService

    async with maker() as session:
        fact = await FactService().upsert(session, key=key, value=value)
        await session.commit()
        return fact.value


async def test_scheduler_and_ledger_writes_under_concurrency(tmp_path):
    engine = _concurrent_engine(tmp_path / "concurrent.db")
    await _schema(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    # Seed a scheduled task (FK target) up front with many concurrent updates.
    from backend.models.scheduled_task import ScheduledTaskRecord
    from backend.models.task_run import TaskRunRecord
    from backend.models.trigger_fire import TriggerFireRecord
    from backend.services.proactive_store import save_scheduled_task

    task = ScheduledTask(id="task-1", name="t")
    async with maker() as session:
        await save_scheduled_task(session, task)
        await session.commit()

    # A monitor trigger id used as the FK target for fire-history rows.
    from backend.engines.monitor import Trigger
    from backend.services.proactive_store import save_monitor_trigger

    trigger = Trigger(id="trigger-1", name="tg")
    async with maker() as session:
        await save_monitor_trigger(session, trigger)
        await session.commit()

    set_ledger_session_factory(lambda: maker)
    try:
        ledger = get_activity_ledger()
        ledger.clear()

        writes = []
        for _ in range(10):
            writes.append(_one_run(maker, task.id))
            writes.append(_one_fire(maker, trigger.id))
            ledger.record_activity(ActivityEntry(
                timestamp=1.0, request_id="r", agent="beru_core",
                tool_name="clock", args_summary="{}", outcome="success",
                duration_ms=1.0, error=None,
            ))
        for i in range(4):
            ledger.record_audit(AuditEntry(
                timestamp=2.0, request_id=f"a{i}", agent="beru_core",
                tool_name="run_command", decision="approved",
                confirmation_id="c", outcome="success", error=None,
            ))
        writes.append(ledger.flush())

        results = await asyncio.gather(*writes, return_exceptions=True)
        assert not [r for r in results if isinstance(r, BaseException)]

        async with maker() as session:
            runs = await session.scalar(select(func.count(TaskRunRecord.id)))
            fires = await session.scalar(select(func.count(TriggerFireRecord.id)))
            tasks = await session.scalar(
                select(func.count(ScheduledTaskRecord.id))
            )
        assert runs == 10
        assert fires == 10
        assert tasks == 1  # 10 concurrent saves collapsed onto one task row

        async with maker() as session:
            ledged = await session.execute(
                select(func.count()).select_from(__import__(
                    "backend.models.activity_record", fromlist=["ActivityRecord"]
                ).ActivityRecord)
            )
            activity_rows = ledged.scalar_one()
        assert activity_rows == 10
    finally:
        await ledger.flush()
        await engine.dispose()
        reset_activity_ledger()


async def test_fact_upsert_same_key_concurrent_single_row(tmp_path):
    engine = _concurrent_engine(tmp_path / "facts.db")
    await _schema(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    values = await asyncio.gather(
        *(_one_fact(maker, "theme", v) for v in ("dark", "light", "dark", "light")),
        return_exceptions=True,
    )
    assert not [v for v in values if isinstance(v, BaseException)]
    # Every writer must resolve to one of the offered values — none raises.
    assert all(v in ("dark", "light") for v in values)

    from backend.models.fact import Fact

    async with maker() as session:
        total = await session.scalar(select(func.count(Fact.id)).where(Fact.key == "theme"))
        final = await session.scalar(select(Fact.value).where(Fact.key == "theme"))
    assert total == 1  # the upsert race never leaves duplicate keys
    assert final in ("dark", "light")
    await engine.dispose()


async def test_embeddings_concurrent_same_source_single_row(tmp_path):
    engine = _concurrent_engine(tmp_path / "embeddings.db")
    await _schema(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    from backend.memory.vector_store import EmbeddingRow, VectorStore

    async def _upsert(text: str, vector):
        async with maker() as session:
            await VectorStore().upsert(
                session, source_table="messages", source_id="m1", text=text,
                vector=vector,
            )
            await session.commit()

    await asyncio.gather(
        _upsert("a", [1.0, 0.0]),
        _upsert("b", [0.0, 1.0]),
        _upsert("c", [1.0, 1.0]),
    )
    async with maker() as session:
        total = await session.scalar(select(func.count(EmbeddingRow.id)))
    assert total == 1
    await engine.dispose()