"""Durability tests for the proactive layer.

Covers the full create -> persist -> restart -> restore -> execute -> update ->
delete lifecycle for both scheduled tasks and monitor triggers, plus the store's
value round-trips and idempotent startup restoration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.engines.monitor import (
    ConditionType,
    Trigger,
    TriggerCondition,
    TriggerStatus,
)
from backend.engines.scheduler import ScheduledTask, TaskStatus, TaskType
from backend.services.proactive_service import (
    get_event_monitor,
    get_scheduler,
    restore_proactive_state,
    set_session_factory,
    start_proactive_runtime,
    stop_proactive_runtime,
)
from backend.services.proactive_store import (
    delete_scheduled_task,
    load_monitor_triggers,
    load_scheduled_tasks,
    save_monitor_trigger,
    save_scheduled_task,
)


@pytest_asyncio.fixture
async def proactive(_sessionmaker):
    """Point the shared runtime at the temp DB and clear its state."""
    set_session_factory(lambda: _sessionmaker)
    get_scheduler().clear()
    get_event_monitor().clear()
    return get_scheduler(), get_event_monitor()


async def _stored_task(session, task_id):
    from backend.models.scheduled_task import ScheduledTaskRecord

    session.expire_all()  # read fresh state, not the identity-map cache
    return await session.get(ScheduledTaskRecord, task_id)


async def _stored_trigger(session, trigger_id):
    from backend.models.monitor_trigger import MonitorTriggerRecord

    session.expire_all()  # read fresh state, not the identity-map cache
    return await session.get(MonitorTriggerRecord, trigger_id)


# ---------------------------------------------------------------------------
# Scheduler task persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_round_trips_through_store(proactive, db_session):
    """save + load preserves every field (schedule, payload, state, counters)."""
    expected_next_run = datetime.now(timezone.utc) + timedelta(seconds=60)
    original = ScheduledTask(
        name="roundtrip",
        description="survives the store",
        handler="notify",
        task_type=TaskType.INTERVAL,
        status=TaskStatus.PAUSED,
        interval_seconds=300.0,
        run_at=None,
        max_runs=5,
        run_count=2,
        next_run=expected_next_run,
        payload={"title": "hi", "nested": {"a": [1, 2, 3]}},
        agent="beru_core",
    )

    await save_scheduled_task(db_session, original)
    # Mutate the in-memory object afterwards: the persisted copy must not change.
    original.run_count = 99
    await db_session.commit()

    (restored,) = await load_scheduled_tasks(db_session)
    assert restored.id == original.id
    assert restored.name == "roundtrip"
    assert restored.description == "survives the store"
    assert restored.handler == "notify"
    assert restored.task_type == TaskType.INTERVAL
    assert restored.status == TaskStatus.PAUSED  # paused: 2, not 99
    assert restored.interval_seconds == 300.0
    assert restored.max_runs == 5
    assert restored.run_count == 2
    assert restored.payload == {"title": "hi", "nested": {"a": [1, 2, 3]}}
    assert restored.agent == "beru_core"
    assert restored.next_run is not None
    # Round-trip through SQLite normalises back to aware UTC.
    assert restored.next_run.tzinfo is not None
    assert restored.next_run == expected_next_run


@pytest.mark.asyncio
async def test_task_save_updates_existing_row_not_duplicate(proactive, db_session):
    """Saving the same task twice keeps one row (upsert by id)."""
    task = ScheduledTask(name="t", handler="notify", task_type=TaskType.ONE_SHOT)
    await save_scheduled_task(db_session, task)
    task.name = "renamed"
    await save_scheduled_task(db_session, task)
    await db_session.commit()

    (restored,) = await load_scheduled_tasks(db_session)
    assert restored.name == "renamed"
    assert len(await load_scheduled_tasks(db_session)) == 1


@pytest.mark.asyncio
async def test_task_delete_removes_persisted_row(proactive, db_session):
    task = ScheduledTask(name="gone", task_type=TaskType.ONE_SHOT)
    await save_scheduled_task(db_session, task)
    await db_session.commit()

    assert await delete_scheduled_task(db_session, task.id) is True
    await db_session.commit()
    assert await load_scheduled_tasks(db_session) == []
    assert await delete_scheduled_task(db_session, task.id) is False


@pytest.mark.asyncio
async def test_api_created_task_is_persisted_default_scheduled(client, db_session):
    """An HTTP-created task appears in the database with a sensible schedule."""
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "persisted-task",
            "handler": "notify",
            "task_type": "interval",
            "interval_seconds": 60,
            "max_runs": 10,
            "payload": {"title": "ping", "message": "hello"},
        },
    )
    assert resp.status_code == 201
    task = resp.json()

    row = await _stored_task(db_session, task["id"])
    assert row is not None
    assert row.name == "persisted-task"
    assert row.task_type == "interval"
    assert row.interval_seconds == 60
    assert row.max_runs == 10
    assert row.payload == {"title": "ping", "message": "hello"}
    assert row.next_run is not None  # add_task scheduled it


# ---------------------------------------------------------------------------
# Monitor trigger persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_round_trips_through_store(proactive, db_session):
    original = Trigger(
        name="disk-watch",
        description="alert on disk pressure",
        status=TriggerStatus.ACTIVE,
        condition=TriggerCondition(
            condition_type=ConditionType.THRESHOLD,
            source="system.disk_usage",
            operator="gt",
            value=85,
            params={"match": "contains"},
        ),
        actions=[{"type": "notify", "title": "Disk full", "message": "85% used"}],
        cooldown_seconds=120.0,
    )

    await save_monitor_trigger(db_session, original)
    await db_session.commit()

    (restored,) = await load_monitor_triggers(db_session)
    assert restored.id == original.id
    assert restored.name == "disk-watch"
    assert restored.status == original.status
    assert restored.condition.condition_type == ConditionType.THRESHOLD
    assert restored.condition.source == "system.disk_usage"
    assert restored.condition.operator == "gt"
    assert restored.condition.value == 85
    assert restored.condition.params == {"match": "contains"}
    assert restored.actions == [
        {"type": "notify", "title": "Disk full", "message": "85% used"}
    ]
    assert restored.cooldown_seconds == 120.0
    assert restored.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_api_created_trigger_is_persisted_and_delete_removes(
    client, db_session, proactive
):
    resp = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "load-spike",
            "condition_type": "threshold",
            "source": "custom.load",
            "operator": "gte",
            "value": 8,
            "actions": [{"type": "notify", "title": "high load", "message": "load >= 8"}],
            "cooldown_seconds": 5,
        },
    )
    assert resp.status_code == 201
    trigger = resp.json()

    row = await _stored_trigger(db_session, trigger["id"])
    assert row is not None
    assert row.name == "load-spike"
    assert row.condition["condition_type"] == "threshold"
    assert row.condition["value"] == 8
    assert row.actions == [{"type": "notify", "title": "high load", "message": "load >= 8"}]
    assert row.cooldown_seconds == 5

    assert (
        await client.delete(f"/api/v1/monitor/triggers/{trigger['id']}")
    ).status_code == 204
    assert await _stored_trigger(db_session, trigger["id"]) is None
    assert await load_monitor_triggers(db_session) == []


# ---------------------------------------------------------------------------
# Restart -> restore (the core durability guarantee)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_restores_persisted_tasks_and_triggers_on_start(proactive, db_session):
    """start_proactive_runtime() reloads everything from the database."""
    scheduler, monitor = proactive

    task = ScheduledTask(
        name="survive", handler="notify", task_type=TaskType.INTERVAL,
        interval_seconds=60, payload={"title": "t", "message": "m"},
    )
    trigger = Trigger(
        name="survive-trigger",
        condition=TriggerCondition(
            condition_type=ConditionType.THRESHOLD,
            source="custom.load", operator="gt", value=5,
        ),
    )
    await save_scheduled_task(db_session, task)
    await save_monitor_trigger(db_session, trigger)
    await db_session.commit()

    # Fresh "process": engines are empty.
    scheduler.clear()
    monitor.clear()
    assert scheduler.list_tasks() == []
    assert monitor.list_triggers() == []

    await start_proactive_runtime()
    try:
        assert [t.id for t in scheduler.list_tasks()] == [task.id]
        assert [t.name for t in scheduler.list_tasks()] == ["survive"]
        assert [t.id for t in monitor.list_triggers()] == [trigger.id]
    finally:
        await stop_proactive_runtime()


@pytest.mark.asyncio
async def test_restore_is_idempotent(proactive, db_session):
    scheduler, monitor = proactive
    task = ScheduledTask(name="once", handler="notify", task_type=TaskType.ONE_SHOT)
    trigger = Trigger(name="once")
    await save_scheduled_task(db_session, task)
    await save_monitor_trigger(db_session, trigger)
    await db_session.commit()

    await restore_proactive_state()
    await restore_proactive_state()

    assert len(scheduler.list_tasks()) == 1
    assert len(monitor.list_triggers()) == 1


@pytest.mark.asyncio
async def test_restored_task_executes_and_persists_counters(proactive, db_session):
    """A restored one-shot notify task runs and writes run_count/status back."""
    scheduler, _ = proactive
    original = ScheduledTask(
        name="resume-me",
        handler="notify",
        task_type=TaskType.ONE_SHOT,
        payload={"title": "back on track", "message": "restored execution"},
    )
    await save_scheduled_task(db_session, original)
    await db_session.commit()

    scheduler.clear()
    await restore_proactive_state()
    (restored,) = scheduler.list_tasks()

    await scheduler.run_task(restored.id)
    assert restored.run_count == 1
    assert restored.status == TaskStatus.COMPLETED
    assert restored.last_run is not None

    row = await _stored_task(db_session, restored.id)
    assert row.run_count == 1
    assert row.status == "completed"
    assert row.last_run is not None


@pytest.mark.asyncio
async def test_restored_interval_task_keeps_schedule_and_catches_up(proactive, db_session):
    """A future-dated interval task resumes on schedule; a past one runs next tick."""
    scheduler, _ = proactive
    future = ScheduledTask(
        name="later", handler="notify", task_type=TaskType.INTERVAL,
        interval_seconds=30, next_run=datetime.now(timezone.utc) + timedelta(hours=1),
        payload={"title": "t", "message": "m"},
    )
    overdue = ScheduledTask(
        name="overdue", handler="notify", task_type=TaskType.INTERVAL,
        interval_seconds=30, next_run=datetime.now(timezone.utc) - timedelta(seconds=5),
        payload={"title": "t", "message": "m"},
    )
    await save_scheduled_task(db_session, future)
    await save_scheduled_task(db_session, overdue)
    await db_session.commit()

    scheduler.clear()
    await restore_proactive_state()

    assert scheduler.get_task(future.id).next_run.tzinfo is not None
    assert scheduler.get_task(future.id).next_run > datetime.now(timezone.utc)
    assert (
        scheduler.get_task(overdue.id).next_run
        < datetime.now(timezone.utc)
    )

    ran = await scheduler.run_due()
    assert [t.name for t in ran] == ["overdue"]
    assert scheduler.get_task(overdue.id).run_count == 1
    assert scheduler.get_task(overdue.id).next_run > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_restored_trigger_fires_and_persists_fire_count(proactive, db_session):
    """After restart a restored trigger still fires; fire_count is persisted."""
    _, monitor = proactive
    original = Trigger(
        name="temp-check",
        condition=TriggerCondition(
            condition_type=ConditionType.THRESHOLD,
            source="reading.temperature",
            operator="gt",
            value=70,
        ),
        actions=[{"type": "notify", "title": "hot", "message": "temperature high"}],
    )
    await save_monitor_trigger(db_session, original)
    await db_session.commit()

    monitor.clear()
    await restore_proactive_state()
    (restored,) = monitor.list_triggers()

    monitor.update_source_value("reading.temperature", 90)
    fired = await monitor.check_once()
    assert restored.id in fired
    assert restored.fire_count == 1
    assert restored.last_fired is not None

    row = await _stored_trigger(db_session, restored.id)
    assert row.fire_count == 1
    # Zero cooldown re-arms immediately, so the persisted status is active.
    assert row.status == "active"
    assert row.last_fired is not None


# ---------------------------------------------------------------------------
# Update operations keep persistent state in sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_resume_cancel_persist_across_restart(client, db_session, proactive):
    """Pause/resume/cancel write through; the DB state survives a restore."""
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "flaky", "handler": "notify", "task_type": "one_shot"},
    )
    task_id = resp.json()["id"]

    await client.post(f"/api/v1/scheduler/tasks/{task_id}/pause")
    assert (await _stored_task(db_session, task_id)).status == "paused"

    await client.post(f"/api/v1/scheduler/tasks/{task_id}/resume")
    assert (await _stored_task(db_session, task_id)).status == "pending"

    await client.post(f"/api/v1/scheduler/tasks/{task_id}/cancel")
    assert (await _stored_task(db_session, task_id)).status == "cancelled"

    # A cancelled task is NOT executed after restore.
    get_scheduler().clear()
    await restore_proactive_state()
    (restored,) = get_scheduler().list_tasks()
    assert restored.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_trigger_status_changes_persist(client, db_session, proactive):
    resp = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "toon",
            "condition_type": "threshold",
            "source": "custom.x",
            "operator": "eq",
            "value": 1,
        },
    )
    trigger_id = resp.json()["id"]

    await client.post(f"/api/v1/monitor/triggers/{trigger_id}/pause")
    assert (await _stored_trigger(db_session, trigger_id)).status == "paused"

    await client.post(f"/api/v1/monitor/triggers/{trigger_id}/resume")
    assert (await _stored_trigger(db_session, trigger_id)).status == "active"

    await client.post(f"/api/v1/monitor/triggers/{trigger_id}/disable")
    assert (await _stored_trigger(db_session, trigger_id)).status == "disabled"

    # A disabled trigger never fires after restore.
    get_event_monitor().clear()
    await restore_proactive_state()
    (restored,) = get_event_monitor().list_triggers()
    get_event_monitor().update_source_value("custom.x", 1)
    assert await get_event_monitor().check_once() == []
    assert restored.fire_count == 0


@pytest.mark.asyncio
async def test_api_delete_removes_task_from_db_and_engine(client, db_session, proactive):
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "bidone", "handler": "notify", "task_type": "one_shot"},
    )
    task_id = resp.json()["id"]

    assert (
        await client.delete(f"/api/v1/scheduler/tasks/{task_id}")
    ).status_code == 204
    assert await _stored_task(db_session, task_id) is None
    assert get_scheduler().get_task(task_id) is None

    # Delete again is a clean 404.
    assert (
        await client.delete(f"/api/v1/scheduler/tasks/{task_id}")
    ).status_code == 404