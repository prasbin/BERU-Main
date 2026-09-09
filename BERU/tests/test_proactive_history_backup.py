"""Tests for audit-history export/restore (backup of the append-only logs).

Covers the store's export/import round-trip, idempotent re-imports, replace
mode, id-less rows, and the ``/history/export`` + ``/history/import`` API
endpoints for both the scheduler run log and the monitor fire log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.services.proactive_service import (
    get_event_monitor,
    get_scheduler,
    set_session_factory,
)
from backend.services.proactive_store import (
    export_task_runs,
    export_trigger_fires,
    import_task_runs,
    import_trigger_fires,
    list_task_runs,
    list_trigger_fires,
    prune_task_runs,
    prune_trigger_fires,
    record_task_run,
    record_trigger_fire,
)


@pytest_asyncio.fixture
async def _proactive(_sessionmaker):
    set_session_factory(lambda: _sessionmaker)
    get_scheduler().clear()
    get_event_monitor().clear()
    return get_scheduler(), get_event_monitor()


# ---------------------------------------------------------------------------
# Store: export / import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_import_round_trip(_proactive, db_session):
    """An export can be restored after the log is emptied, preserving rows."""
    _, _ = _proactive
    await record_task_run(
        db_session, task_id="t", status="completed", run_count=1, duration_ms=5.0
    )
    await record_task_run(
        db_session,
        task_id="t",
        status="failed",
        run_count=2,
        duration_ms=7.5,
        error="boom",
    )
    await record_trigger_fire(
        db_session,
        trigger_id="tr",
        condition={"condition_type": "threshold", "source": "x", "value": 5},
        value=9,
    )
    await db_session.commit()

    runs = await export_task_runs(db_session)
    fires = await export_trigger_fires(db_session)
    assert len(runs) == 2
    assert len(fires) == 1
    exported_ids = {r["id"] for r in runs}

    await prune_task_runs(db_session, "t", keep_last=0)
    await prune_trigger_fires(db_session, "tr", keep_last=0)
    await db_session.commit()
    assert await export_task_runs(db_session) == []
    assert await export_trigger_fires(db_session) == []

    result = await import_task_runs(db_session, runs)
    assert result == {"inserted": 2, "skipped": 0, "replaced": 0}
    fire_result = await import_trigger_fires(db_session, fires)
    assert fire_result == {"inserted": 1, "skipped": 0, "replaced": 0}
    await db_session.commit()

    restored = await list_task_runs(db_session, "t")
    assert len(restored) == 2
    assert {r.id for r in restored} == exported_ids
    assert {r.error for r in restored if r.status == "failed"} == {"boom"}
    assert {r.duration_ms for r in restored} == {5.0, 7.5}

    restored_fire = await list_trigger_fires(db_session, "tr")
    assert len(restored_fire) == 1
    assert restored_fire[0].condition["source"] == "x"
    assert restored_fire[0].value == 9


@pytest.mark.asyncio
async def test_import_is_idempotent(_proactive, db_session):
    """Re-importing the same document skips rows that already exist."""
    _, _ = _proactive
    await record_task_run(db_session, task_id="t", status="completed", run_count=1)
    await db_session.commit()

    doc = await export_task_runs(db_session)
    await prune_task_runs(db_session, "t", keep_last=0)
    await db_session.commit()
    first = await import_task_runs(db_session, doc)
    second = await import_task_runs(db_session, doc)
    await db_session.commit()
    assert first == {"inserted": 1, "skipped": 0, "replaced": 0}
    assert second == {"inserted": 0, "skipped": 1, "replaced": 0}
    assert len(await list_task_runs(db_session, "t")) == 1


@pytest.mark.asyncio
async def test_import_replace_overwrites_existing(_proactive, db_session):
    """replace=True overwrites a row whose id already exists."""
    _, _ = _proactive
    await record_task_run(
        db_session, task_id="t", status="completed", run_count=1, duration_ms=5.0
    )
    await db_session.commit()

    doc = await export_task_runs(db_session)
    doc[0]["duration_ms"] = 99.0
    doc[0]["status"] = "failed"
    doc[0]["error"] = "new error"

    replaced = await import_task_runs(db_session, doc, replace=True)
    await db_session.commit()
    assert replaced == {"inserted": 0, "skipped": 0, "replaced": 1}

    row = (await list_task_runs(db_session, "t"))[0]
    assert row.duration_ms == 99.0
    assert row.status == "failed"
    assert row.error == "new error"


@pytest.mark.asyncio
async def test_import_rows_without_ids_get_fresh_ids(_proactive, db_session):
    """Id-less rows are inserted with a new generated id."""
    _, _ = _proactive
    result = await import_task_runs(
        db_session,
        [{"task_id": "t", "status": "completed", "run_count": 1, "run_at": (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()}],
    )
    await db_session.commit()
    assert result == {"inserted": 1, "skipped": 0, "replaced": 0}
    rows = await list_task_runs(db_session, "t")
    assert len(rows) == 1
    assert rows[0].id


# ---------------------------------------------------------------------------
# API: export / import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_export_api(client, _proactive):
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "exporter",
            "handler": "notify",
            "task_type": "one_shot",
            "payload": {"title": "t", "message": "m"},
        },
    )
    task_id = resp.json()["id"]
    for _ in range(2):
        await client.post(f"/api/v1/scheduler/tasks/{task_id}/run")

    data = (await client.get("/api/v1/scheduler/history/export")).json()
    assert data["kind"] == "task_runs"
    assert data["count"] == 2
    assert {r["task_id"] for r in data["rows"]} == {task_id}


@pytest.mark.asyncio
async def test_history_import_api_restores_after_prune(client, _proactive):
    """Export, wipe, restore: the log comes back exactly as it was."""
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "backup-me",
            "handler": "notify",
            "task_type": "one_shot",
            "payload": {"title": "t", "message": "m"},
        },
    )
    task_id = resp.json()["id"]
    for _ in range(2):
        await client.post(f"/api/v1/scheduler/tasks/{task_id}/run")

    exported = (await client.get("/api/v1/scheduler/history/export")).json()["rows"]
    assert len(exported) == 2

    pruned = await client.post(
        f"/api/v1/scheduler/tasks/{task_id}/prune-runs", json={"keep_last": 0}
    )
    assert pruned.json()["remaining"] == 0

    imported = (
        await client.post(
            "/api/v1/scheduler/history/import",
            json={"rows": exported, "replace": False},
        )
    ).json()
    assert imported == {"inserted": 2, "skipped": 0, "replaced": 0}

    data = (await client.get(f"/api/v1/scheduler/tasks/{task_id}/runs")).json()
    assert data["total"] == 2
    assert data["summary"]["total"] == 2


@pytest.mark.asyncio
async def test_fires_export_import_api(client, _proactive):
    resp = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "fire-backup",
            "condition_type": "threshold",
            "source": "custom.load",
            "operator": "gt",
            "value": 3,
        },
    )
    trigger_id = resp.json()["id"]
    await client.post("/api/v1/monitor/sources/custom.load", json={"value": 7})
    for _ in range(2):
        tick = await client.post("/api/v1/monitor/tick")
        assert trigger_id in tick.json()["fired"]

    exported = (await client.get("/api/v1/monitor/history/export")).json()["rows"]
    assert len(exported) == 2

    await client.post(
        f"/api/v1/monitor/triggers/{trigger_id}/prune-fires", json={"keep_last": 0}
    )
    assert (await client.get(f"/api/v1/monitor/triggers/{trigger_id}/fires")).json()["total"] == 0

    imported = (
        await client.post(
            "/api/v1/monitor/history/import",
            json={"rows": exported, "replace": False},
        )
    ).json()
    assert imported == {"inserted": 2, "skipped": 0, "replaced": 0}
    assert (await client.get(f"/api/v1/monitor/triggers/{trigger_id}/fires")).json()["total"] == 2