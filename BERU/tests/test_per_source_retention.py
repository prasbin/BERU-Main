"""Tests for per-source audit retention policies.

Covers:
* PATCH endpoints for tasks and triggers with ``retention_days`` / ``keep_last``.
* ``prune_old_history`` sweep respects per-source ``retention_days`` and
  ``keep_last``, falling back to the global ``BERU_PROACTIVE_AUDIT_RETENTION_DAYS``
  when the per-source field is ``None``.
* ``prune_task_runs`` / ``prune_trigger_fires`` helpers honour ``keep_last``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio

from backend.core.config import get_settings
from backend.engines.monitor import Trigger, TriggerCondition
from backend.engines.scheduler import ScheduledTask, TaskType
from backend.services.proactive_service import (
    get_event_monitor,
    get_scheduler,
    prune_old_history,
    set_session_factory,
)
from backend.services.proactive_store import (
    count_task_runs,
    count_trigger_fires,
    prune_task_runs,
    prune_trigger_fires,
    record_task_run,
    record_trigger_fire,
)


@pytest_asyncio.fixture
async def _proactive(_sessionmaker):
    """Point the shared proactive runtime at the temp DB and clear its state."""
    set_session_factory(lambda: _sessionmaker)
    get_scheduler().clear()
    get_event_monitor().clear()
    return get_scheduler(), get_event_monitor()


# ---------------------------------------------------------------------------
# Store-level: prune_task_runs / prune_trigger_fires honour keep_last
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_task_runs_keep_last(_proactive, db_session):
    """keep_last retains exactly N newest rows, deleting older ones."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="pruned",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            keep_last=3,
        )
    )
    for i in range(5):
        await record_task_run(
            db_session,
            task_id=task.id,
            status="completed",
            run_count=i + 1,
            duration_ms=float(i),
            run_at=datetime.now(timezone.utc) + timedelta(seconds=i),
        )
    await db_session.commit()

    assert await count_task_runs(db_session, task.id) == 5
    removed = await prune_task_runs(db_session, task.id, keep_last=task.keep_last)
    assert removed == 2
    assert await count_task_runs(db_session, task.id) == 3


@pytest.mark.asyncio
async def test_prune_trigger_fires_keep_last(_proactive, db_session):
    """keep_last retains the newest N fire-history rows for a trigger."""
    _, monitor = _proactive
    trigger = monitor.add_trigger(
        Trigger(
            name="firy",
            condition=TriggerCondition(source="x"),
            keep_last=2,
        )
    )
    for i in range(4):
        await record_trigger_fire(
            db_session,
            trigger_id=trigger.id,
            condition={"source": "x"},
            value=i,
            fired_at=datetime.now(timezone.utc) + timedelta(seconds=i),
        )
    await db_session.commit()

    assert await count_trigger_fires(db_session, trigger.id) == 4
    removed = await prune_trigger_fires(db_session, trigger.id, keep_last=trigger.keep_last)
    assert removed == 2
    assert await count_trigger_fires(db_session, trigger.id) == 2


# ---------------------------------------------------------------------------
# Store-level: prune respects retention_seconds AND keep_last together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_task_runs_both_policies(_proactive, db_session):
    """When both retention_seconds and keep_last are given, either can prune."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="dual",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            keep_last=2,
        )
    )
    base = datetime.now(timezone.utc)
    # Two old rows (beyond retention) and three recent ones.
    for i, dt_offset in enumerate([
        timedelta(hours=-3),
        timedelta(hours=-2),
        timedelta(seconds=0),
        timedelta(seconds=1),
        timedelta(seconds=2),
    ]):
        await record_task_run(
            db_session,
            task_id=task.id,
            status="completed",
            run_count=i + 1,
            duration_ms=float(i),
            run_at=base + dt_offset,
        )
    await db_session.commit()

    # retention_seconds=3600 (1 hour) drops the two old rows; then
    # keep_last=2 keeps only the newest 2, dropping one more → 3 removed.
    assert await count_task_runs(db_session, task.id) == 5
    removed = await prune_task_runs(
        db_session, task.id, retention_seconds=3600.0, keep_last=2
    )
    assert removed == 3
    assert await count_task_runs(db_session, task.id) == 2


# ---------------------------------------------------------------------------
# Sweep-level: prune_old_history respects per-source settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_uses_per_task_keep_last(_proactive, db_session):
    """prune_old_history applies a task's own keep_last, not the global default."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="swept",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            keep_last=2,
            # No retention_days — sweep should still run keep_last.
        )
    )
    for i in range(4):
        await record_task_run(
            db_session,
            task_id=task.id,
            status="completed",
            run_count=i + 1,
            duration_ms=float(i),
            run_at=datetime.now(timezone.utc) + timedelta(seconds=i),
        )
    await db_session.commit()

    assert await count_task_runs(db_session, task.id) == 4
    # prune_old_history requires global_days > 0, so we temporarily patch it.
    settings = get_settings()
    with patch.object(settings, "proactive_audit_retention_days", 90):
        removed = await prune_old_history()

    assert removed == 2
    assert await count_task_runs(db_session, task.id) == 2


@pytest.mark.asyncio
async def test_sweep_uses_per_trigger_retention_days(_proactive, db_session):
    """prune_old_history applies a trigger's own retention_days."""
    _, monitor = _proactive
    trigger = monitor.add_trigger(
        Trigger(
            name="old-trigger",
            condition=TriggerCondition(source="x"),
            retention_days=0.0001,  # ~8.6 seconds
        )
    )
    base = datetime.now(timezone.utc)
    # One row older than retention_days=0.0001, one fresh.
    await record_trigger_fire(
        db_session,
        trigger_id=trigger.id,
        fired_at=base - timedelta(minutes=1),
        condition={"source": "x"},
        value="old",
    )
    await record_trigger_fire(
        db_session,
        trigger_id=trigger.id,
        fired_at=base,
        condition={"source": "x"},
        value="new",
    )
    await db_session.commit()

    settings = get_settings()
    with patch.object(settings, "proactive_audit_retention_days", 90):
        removed = await prune_old_history()

    assert removed == 1
    assert await count_trigger_fires(db_session, trigger.id) == 1


@pytest.mark.asyncio
async def test_sweep_null_falls_back_to_global(_proactive, db_session):
    """A source with retention_days=None uses the global default."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="global-fallback",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            # retention_days=None, keep_last=None — uses global.
        )
    )
    base = datetime.now(timezone.utc)
    # Old row (2 hours ago), fresh row.
    await record_task_run(
        db_session,
        task_id=task.id,
        status="completed",
        run_count=1,
        duration_ms=1.0,
        run_at=base - timedelta(hours=2),
    )
    await record_task_run(
        db_session,
        task_id=task.id,
        status="completed",
        run_count=2,
        duration_ms=2.0,
        run_at=base,
    )
    await db_session.commit()

    # Global retention = 1 hour; the old row is 2 hours old → pruned.
    settings = get_settings()
    with patch.object(settings, "proactive_audit_retention_days", 1 / 24):
        removed = await prune_old_history()

    assert removed == 1
    assert await count_task_runs(db_session, task.id) == 1


# ---------------------------------------------------------------------------
# API-level: PATCH endpoints persist retention fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_patch_retention_fields(client, _proactive):
    """PATCH on a task persists retention_days and keep_last."""
    create = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "patch-me", "handler": "notify", "task_type": "one_shot"},
    )
    assert create.status_code == 201
    task_id = create.json()["id"]
    assert create.json()["retention_days"] is None
    assert create.json()["keep_last"] is None

    patch_resp = await client.patch(
        f"/api/v1/scheduler/tasks/{task_id}",
        json={"retention_days": 30.0, "keep_last": 100},
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["retention_days"] == 30.0
    assert body["keep_last"] == 100

    # GET confirms persistence.
    get_resp = await client.get(f"/api/v1/scheduler/tasks/{task_id}")
    assert get_resp.json()["retention_days"] == 30.0
    assert get_resp.json()["keep_last"] == 100


@pytest.mark.asyncio
async def test_task_patch_clears_retention(client, _proactive):
    """PATCH with null retention_days / keep_last clears the override."""
    create = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "clear-me",
            "handler": "notify",
            "task_type": "one_shot",
            "retention_days": 10.0,
            "keep_last": 50,
        },
    )
    assert create.status_code == 201
    task_id = create.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/scheduler/tasks/{task_id}",
        json={"retention_days": None, "keep_last": None},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["retention_days"] is None
    assert patch_resp.json()["keep_last"] is None


@pytest.mark.asyncio
async def test_trigger_patch_retention_fields(client, _proactive):
    """PATCH on a trigger persists retention_days and keep_last."""
    create = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "patch-trigger",
            "source": "cpu",
            "condition_type": "threshold",
            "operator": "gte",
            "value": 90,
        },
    )
    assert create.status_code == 201
    trigger_id = create.json()["id"]
    assert create.json()["retention_days"] is None
    assert create.json()["keep_last"] is None

    patch_resp = await client.patch(
        f"/api/v1/monitor/triggers/{trigger_id}",
        json={"retention_days": 7.0, "keep_last": 200},
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["retention_days"] == 7.0
    assert body["keep_last"] == 200

    get_resp = await client.get(f"/api/v1/monitor/triggers/{trigger_id}")
    assert get_resp.json()["retention_days"] == 7.0
    assert get_resp.json()["keep_last"] == 200


@pytest.mark.asyncio
async def test_trigger_create_with_retention(client, _proactive):
    """Creating a trigger with retention fields persists them from day one."""
    create = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "retention-trigger",
            "source": "cpu",
            "condition_type": "threshold",
            "operator": "gte",
            "value": 80,
            "retention_days": 14.0,
            "keep_last": 500,
        },
    )
    assert create.status_code == 201
    body = create.json()
    assert body["retention_days"] == 14.0
    assert body["keep_last"] == 500
