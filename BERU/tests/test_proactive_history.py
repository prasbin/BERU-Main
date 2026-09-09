"""Tests for the run/fire audit-history layer.

Covers the engine hooks (scheduler run hook, monitor fire hook), the store's
append/list functions, the API endpoints that expose history, and that history
is durable and survives a runtime restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.engines.monitor import ConditionType, EventMonitor, Trigger, TriggerCondition
from backend.engines.notifications import NotificationService
from backend.engines.scheduler import ScheduledTask, Scheduler, TaskStatus, TaskType
from backend.services.proactive_service import (
    get_event_monitor,
    get_scheduler,
    prune_old_history,
    restore_proactive_state,
    set_session_factory,
)
from backend.services.proactive_store import (
    count_task_runs,
    count_trigger_fires,
    list_task_runs,
    list_trigger_fires,
    prune_task_runs,
    prune_trigger_fires,
    record_task_run,
    record_trigger_fire,
    task_run_summary,
    task_run_to_dict,
    trigger_fire_summary,
    trigger_fire_to_dict,
)


@pytest_asyncio.fixture
async def _proactive(_sessionmaker):
    set_session_factory(lambda: _sessionmaker)
    get_scheduler().clear()
    get_event_monitor().clear()
    return get_scheduler(), get_event_monitor()


async def async_noop(*_args, **_kwargs) -> None:
    """A handler/hook that does nothing but succeed."""


# ---------------------------------------------------------------------------
# Engine hooks (standalone engines)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_run_hook_fires_after_success():
    scheduler = Scheduler()
    scheduler.register_handler("ok", async_noop)
    seen: list[tuple[str, float]] = []

    async def _capture(task, duration_ms) -> None:
        seen.append((task.status.value, duration_ms))

    scheduler.set_run_hook(_capture)
    task = scheduler.add_task(
        ScheduledTask(name="t", handler="ok", task_type=TaskType.ONE_SHOT)
    )
    await scheduler.run_task(task.id)
    assert task.status == TaskStatus.COMPLETED
    assert len(seen) == 1
    status, duration_ms = seen[0]
    assert status == "completed"
    assert duration_ms >= 0.0


@pytest.mark.asyncio
async def test_scheduler_run_hook_records_failure():
    async def boom(task) -> None:
        raise RuntimeError("handler exploded")

    scheduler = Scheduler()
    scheduler.register_handler("boom", boom)
    seen: list[str] = []

    async def _capture(task, duration_ms) -> None:
        seen.append(task.status.value)

    scheduler.set_run_hook(_capture)
    task = scheduler.add_task(
        ScheduledTask(name="bad", handler="boom", task_type=TaskType.ONE_SHOT)
    )
    await scheduler.run_task(task.id)
    assert task.status == TaskStatus.FAILED
    assert task.last_error == "handler exploded"
    assert seen == ["failed"]


@pytest.mark.asyncio
async def test_monitor_fire_hook_receives_satisfying_value():
    monitor = EventMonitor(NotificationService())
    seen: list[tuple[str, object]] = []

    async def _capture(trigger, value) -> None:
        seen.append((trigger.id, value))

    monitor.set_fire_hook(_capture)
    monitor.update_source_value("custom.load", 12)
    trigger = monitor.add_trigger(
        Trigger(
            name="load",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="custom.load",
                operator="gt",
                value=5,
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired
    assert seen == [(trigger.id, 12)]


# ---------------------------------------------------------------------------
# Store append/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_list_task_runs(_proactive, db_session):
    _, _ = _proactive  # owns engine state; store only needs the session
    task_id = "task-1"
    await record_task_run(
        db_session, task_id=task_id, status="completed", run_count=1, duration_ms=5.0
    )
    await record_task_run(
        db_session, task_id=task_id, status="failed", run_count=1, error="boom"
    )
    await db_session.commit()

    rows = await list_task_runs(db_session, task_id)
    assert len(rows) == 2
    # Newest first.
    assert [r.status for r in rows] == ["failed", "completed"]
    failed = rows[0]
    assert failed.duration_ms is None
    assert failed.error == "boom"
    assert failed.run_count == 1

    d = task_run_to_dict(failed)
    assert d["task_id"] == "task-1"
    assert d["status"] == "failed"
    assert d["error"] == "boom"
    assert d["run_at"] is not None

    assert await list_task_runs(db_session, "other-task") == []


@pytest.mark.asyncio
async def test_list_task_runs_respects_limit(_proactive, db_session):
    _, _ = _proactive
    base = datetime.now(timezone.utc)
    for i in range(5):
        await record_task_run(
            db_session,
            task_id="t",
            status="completed",
            run_count=i + 1,
            run_at=base + timedelta(seconds=i),
        )
    await db_session.commit()

    rows = await list_task_runs(db_session, "t", limit=2)
    assert len(rows) == 2
    assert [r.run_count for r in rows] == [5, 4]


@pytest.mark.asyncio
async def test_record_and_list_trigger_fires(_proactive, db_session):
    _, _ = _proactive
    condition = {"condition_type": "threshold", "source": "x", "value": 5}
    await record_trigger_fire(
        db_session, trigger_id="tr-1", condition=condition, value=10
    )
    await db_session.commit()

    rows = await list_trigger_fires(db_session, "tr-1")
    assert len(rows) == 1
    assert rows[0].condition == condition
    assert rows[0].value == 10
    assert rows[0].fired_at.tzinfo is not None

    d = trigger_fire_to_dict(rows[0])
    assert d["trigger_id"] == "tr-1"
    assert d["condition"]["source"] == "x"
    assert d["value"] == 10
    assert d["fired_at"] is not None

    assert await list_trigger_fires(db_session, "unknown") == []


# ---------------------------------------------------------------------------
# Runtime wiring: executions and firings append history rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_run_appends_history_row(_proactive, db_session):
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="rh",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            payload={"title": "t", "message": "m"},
        )
    )
    await scheduler.run_task(task.id)

    rows = await list_task_runs(db_session, task.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "completed"
    assert row.run_count == 1
    assert row.error is None


@pytest.mark.asyncio
async def test_runtime_trigger_fire_appends_history_row(_proactive, db_session):
    _, monitor = _proactive
    trigger = monitor.add_trigger(
        Trigger(
            name="th",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="custom.load",
                operator="gt",
                value=3,
            ),
        )
    )
    monitor.update_source_value("custom.load", 9)
    fired = await monitor.check_once()
    assert trigger.id in fired

    rows = await list_trigger_fires(db_session, trigger.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.value == 9
    assert row.condition["source"] == "custom.load"
    assert row.condition["condition_type"] == "threshold"


@pytest.mark.asyncio
async def test_history_survives_restart(_proactive, db_session):
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="durable",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            payload={"title": "t", "message": "m"},
        )
    )
    await scheduler.run_task(task.id)

    scheduler.clear()
    await restore_proactive_state()

    assert [t.id for t in scheduler.list_tasks()] == [task.id]
    rows = await list_task_runs(db_session, task.id)
    assert len(rows) == 1
    assert rows[0].status == "completed"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_runs_api(client, _proactive):
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "hist-api",
            "handler": "notify",
            "task_type": "one_shot",
            "payload": {"title": "t", "message": "m"},
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    assert (await client.post(f"/api/v1/scheduler/tasks/{task_id}/run")).status_code == 200

    data = (await client.get(f"/api/v1/scheduler/tasks/{task_id}/runs")).json()
    assert data["task_id"] == task_id
    assert data["total"] == 1
    run = data["runs"][0]
    assert run["status"] == "completed"
    assert run["run_count"] == 1
    assert run["error"] is None

    assert (await client.get("/api/v1/scheduler/tasks/nope/runs")).status_code == 404


@pytest.mark.asyncio
async def test_trigger_fires_api(client, _proactive):
    resp = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "hist-trigger",
            "condition_type": "threshold",
            "source": "custom.load",
            "operator": "gt",
            "value": 3,
        },
    )
    assert resp.status_code == 201
    trigger_id = resp.json()["id"]

    await client.post("/api/v1/monitor/sources/custom.load", json={"value": 7})
    tick = await client.post("/api/v1/monitor/tick")
    assert tick.json()["fired"] == [trigger_id]

    data = (await client.get(f"/api/v1/monitor/triggers/{trigger_id}/fires")).json()
    assert data["trigger_id"] == trigger_id
    assert data["total"] == 1
    fire = data["fires"][0]
    assert fire["value"] == 7
    assert fire["condition"]["source"] == "custom.load"
    assert fire["fired_at"] is not None

    assert (await client.get("/api/v1/monitor/triggers/nope/fires")).status_code == 404


@pytest.mark.asyncio
async def test_runs_endpoint_404_after_task_delete(client, _proactive):
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "gone", "handler": "notify", "task_type": "one_shot"},
    )
    task_id = resp.json()["id"]
    await client.post(f"/api/v1/scheduler/tasks/{task_id}/run")
    assert (await client.delete(f"/api/v1/scheduler/tasks/{task_id}")).status_code == 204
    assert (await client.get(f"/api/v1/scheduler/tasks/{task_id}/runs")).status_code == 404


# ---------------------------------------------------------------------------
# History summaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_run_summary_aggregates(_proactive, db_session):
    _, _ = _proactive
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i, duration in enumerate((10.0, 20.0, 30.0), start=1):
        await record_task_run(
            db_session,
            task_id="t",
            status="completed",
            run_count=i,
            duration_ms=duration,
            run_at=base + timedelta(seconds=i),
        )
    await record_task_run(
        db_session,
        task_id="t",
        status="failed",
        run_count=4,
        duration_ms=40.0,
        error="boom",
        run_at=base + timedelta(seconds=4),
    )
    await db_session.commit()

    summary = await task_run_summary(db_session, "t")
    assert summary["total"] == 4
    assert summary["failed"] == 1
    assert summary["success_rate"] == 0.75
    assert summary["failure_rate"] == 0.25
    assert summary["last_error"] == "boom"
    assert summary["avg_duration_ms"] == 25.0
    assert summary["min_duration_ms"] == 10.0
    assert summary["max_duration_ms"] == 40.0
    assert summary["first_run"] is not None
    assert summary["last_run"] is not None

    assert await count_task_runs(db_session, "t") == 4
    empty = await task_run_summary(db_session, "other")
    assert empty["total"] == 0
    assert empty["failed"] == 0
    assert empty["success_rate"] is None
    assert empty["failure_rate"] is None
    assert empty["last_error"] is None
    assert empty["avg_duration_ms"] is None


@pytest.mark.asyncio
async def test_trigger_fire_summary_aggregates(_proactive, db_session):
    _, _ = _proactive
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for offset in (0, 100):
        await record_trigger_fire(
            db_session,
            trigger_id="tr",
            fired_at=base + timedelta(seconds=offset),
            condition={"source": "x"},
            value=offset,
        )
    await db_session.commit()

    summary = await trigger_fire_summary(db_session, "tr")
    assert summary["total"] == 2
    assert summary["first_fired"] is not None
    assert summary["last_fired"] is not None
    assert await count_trigger_fires(db_session, "tr") == 2
    assert (await trigger_fire_summary(db_session, "unknown"))["total"] == 0


# ---------------------------------------------------------------------------
# History pruning / retention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_task_runs_by_retention(_proactive, db_session):
    _, _ = _proactive
    base = datetime.now(timezone.utc) - timedelta(hours=10)
    await record_task_run(
        db_session, task_id="t", status="completed", run_count=1, run_at=base
    )
    await record_task_run(
        db_session,
        task_id="t",
        status="completed",
        run_count=2,
        run_at=base + timedelta(hours=5),
    )
    await record_task_run(
        db_session,
        task_id="t",
        status="completed",
        run_count=3,
        run_at=base + timedelta(hours=9),
    )
    await db_session.commit()

    # 2h retention keeps only the newest run (now-1h); the older two go.
    deleted = await prune_task_runs(db_session, "t", retention_seconds=2 * 3600)
    await db_session.commit()
    assert deleted == 2
    remaining = await list_task_runs(db_session, "t")
    assert [r.run_count for r in remaining] == [3]


@pytest.mark.asyncio
async def test_prune_task_runs_keep_last(_proactive, db_session):
    _, _ = _proactive
    base = datetime.now(timezone.utc)
    for i in range(5):
        await record_task_run(
            db_session,
            task_id="t",
            status="completed",
            run_count=i + 1,
            run_at=base + timedelta(seconds=i),
        )
    await db_session.commit()

    deleted = await prune_task_runs(db_session, "t", keep_last=2)
    await db_session.commit()
    assert deleted == 3
    assert [r.run_count for r in await list_task_runs(db_session, "t")] == [5, 4]


@pytest.mark.asyncio
async def test_prune_task_runs_keep_last_zero_deletes_all(_proactive, db_session):
    _, _ = _proactive
    now = datetime.now(timezone.utc)
    for i in range(3):
        await record_task_run(
            db_session,
            task_id="t",
            status="completed",
            run_count=i + 1,
            run_at=now + timedelta(seconds=i),
        )
    await db_session.commit()

    deleted = await prune_task_runs(db_session, "t", keep_last=0)
    await db_session.commit()
    assert deleted == 3
    assert await count_task_runs(db_session, "t") == 0


@pytest.mark.asyncio
async def test_prune_trigger_fires_keep_last(_proactive, db_session):
    _, _ = _proactive
    base = datetime.now(timezone.utc)
    for i in range(3):
        await record_trigger_fire(
            db_session,
            trigger_id="tr",
            fired_at=base + timedelta(seconds=i),
            condition={"source": "x"},
            value=i,
        )
    await db_session.commit()

    deleted = await prune_trigger_fires(db_session, "tr", keep_last=1)
    await db_session.commit()
    assert deleted == 2
    remaining = await list_trigger_fires(db_session, "tr")
    assert len(remaining) == 1
    assert remaining[0].value == 2


@pytest.mark.asyncio
async def test_prune_runs_api(client, _proactive):
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "prune-me",
            "handler": "notify",
            "task_type": "one_shot",
            "payload": {"title": "t", "message": "m"},
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    for _ in range(3):
        assert (
            await client.post(f"/api/v1/scheduler/tasks/{task_id}/run")
        ).status_code == 200

    data = (await client.get(f"/api/v1/scheduler/tasks/{task_id}/runs")).json()
    assert data["total"] == 3
    assert data["summary"]["total"] == 3
    assert data["summary"]["success_rate"] == 1.0

    pruned = await client.post(
        f"/api/v1/scheduler/tasks/{task_id}/prune-runs", json={"keep_last": 1}
    )
    assert pruned.status_code == 200
    assert pruned.json()["deleted"] == 2
    assert pruned.json()["remaining"] == 1

    assert (await client.get(f"/api/v1/scheduler/tasks/{task_id}/runs")).json()["total"] == 1
    assert (
        await client.post(f"/api/v1/scheduler/tasks/{task_id}/prune-runs", json={})
    ).status_code == 422
    assert (
        await client.post(
            f"/api/v1/scheduler/tasks/{task_id}/prune-runs", json={"keep_last": -1}
        )
    ).status_code == 422
    assert (
        await client.post("/api/v1/scheduler/tasks/nope/prune-runs", json={"keep_last": 1})
    ).status_code == 404


@pytest.mark.asyncio
async def test_prune_fires_api(client, _proactive):
    resp = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "prune-fires",
            "condition_type": "threshold",
            "source": "custom.load",
            "operator": "gt",
            "value": 3,
        },
    )
    assert resp.status_code == 201
    trigger_id = resp.json()["id"]

    await client.post("/api/v1/monitor/sources/custom.load", json={"value": 7})
    for _ in range(3):
        tick = await client.post("/api/v1/monitor/tick")
        assert trigger_id in tick.json()["fired"]

    data = (await client.get(f"/api/v1/monitor/triggers/{trigger_id}/fires")).json()
    assert data["total"] == 3
    assert data["summary"]["total"] == 3

    pruned = await client.post(
        f"/api/v1/monitor/triggers/{trigger_id}/prune-fires", json={"keep_last": 1}
    )
    assert pruned.status_code == 200
    assert pruned.json()["deleted"] == 2
    assert pruned.json()["remaining"] == 1

    assert (
        await client.post("/api/v1/monitor/triggers/nope/prune-fires", json={"keep_last": 1})
    ).status_code == 404


@pytest.mark.asyncio
async def test_prune_old_history_honours_retention(monkeypatch, _proactive, db_session):
    """BERU_PROACTIVE_AUDIT_RETENTION_DAYS prunes old history at runtime start."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="old", handler="notify", task_type=TaskType.ONE_SHOT,
            payload={"title": "t", "message": "m"},
        )
    )
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    await record_task_run(
        db_session, task_id=task.id, run_at=base, status="completed", run_count=1
    )
    await record_task_run(
        db_session, task_id=task.id, status="completed", run_count=2
    )
    await db_session.commit()

    from backend.core.config import get_settings

    monkeypatch.setenv("BERU_PROACTIVE_AUDIT_RETENTION_DAYS", "0.01")  # ~15 minutes
    get_settings.cache_clear()
    try:
        removed = await prune_old_history()
    finally:
        get_settings.cache_clear()

    assert removed == 1
    remaining = await list_task_runs(db_session, task.id)
    assert [r.run_count for r in remaining] == [2]


@pytest.mark.asyncio
async def test_prune_old_history_off_by_default(_proactive, db_session):
    """Without a retention setting, startup pruning removes nothing."""
    from backend.core.config import get_settings

    assert get_settings().proactive_audit_retention_days == 0
    assert await prune_old_history() == 0


# ---------------------------------------------------------------------------
# Per-failure error grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_run_summary_groups_failures_by_error(_proactive, db_session):
    _, _ = _proactive
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    await record_task_run(
        db_session, task_id="t", status="completed", run_count=1, duration_ms=2.0,
        run_at=base,
    )
    for i, error in enumerate(
        ("timeout on retry", "timeout on retry", "connection refused"), start=2
    ):
        await record_task_run(
            db_session, task_id="t", status="failed", run_count=i, error=error,
            run_at=base + timedelta(seconds=i),
        )
    await db_session.commit()

    summary = await task_run_summary(db_session, "t")
    groups = summary["error_groups"]
    assert [g["error"] for g in groups] == ["timeout on retry", "connection refused"]
    assert groups[0]["count"] == 2
    assert groups[1]["count"] == 1
    assert groups[0]["last_run"] is not None
    assert summary["failed"] == 3

    empty = await task_run_summary(db_session, "other")
    assert empty["error_groups"] == []


@pytest.mark.asyncio
async def test_runs_api_surfaces_error_groups(client, _proactive, db_session):
    from backend.services.proactive_store import record_task_run

    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "errors", "handler": "notify", "task_type": "one_shot"},
    )
    task_id = resp.json()["id"]
    for error in ("boom A", "boom A"):
        await record_task_run(
            db_session, task_id=task_id, status="failed", run_count=1, error=error
        )
    await db_session.commit()

    data = (await client.get(f"/api/v1/scheduler/tasks/{task_id}/runs")).json()
    groups = data["summary"]["error_groups"]
    assert [g["error"] for g in groups] == ["boom A"]
    assert groups[0]["count"] == 2
    assert data["summary"]["failed"] == 2