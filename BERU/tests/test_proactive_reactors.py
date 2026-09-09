"""Tests for reactor notifications and history.* trigger sources.

Covers the roadmap's "recommended next step":
* ``history.*`` scoped monitor sources let triggers react to a task's (or their
  own) track record: ``history.task_failure_rate:<id>``,
  ``history.task_total_runs:<id>``, ``history.task_last_error:<id>``,
  ``history.task_error_groups:<id>``, ``history.trigger_fire_count:<id>`` and
  ``history.trigger_last_value:<id>``.
* The new ``reactor`` scheduled-task action type streams a task's run result
  (status, duration, error, error-group count) to the inbox — a reactor task
  listens to another task's run results and emits richer notifications than a
  summary.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from backend.engines.monitor import ConditionType, Trigger, TriggerCondition
from backend.engines.scheduler import ScheduledTask, TaskStatus, TaskType
from backend.services.proactive_service import (
    get_event_monitor,
    get_notification_service,
    get_scheduler,
    set_session_factory,
)
from backend.services.proactive_store import (
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
# history.* source registration
# ---------------------------------------------------------------------------


def test_monitor_registers_history_sources(_proactive):
    """The shared monitor exposes the history scoped sources."""
    _, monitor = _proactive
    sources = set(monitor.list_sources())
    assert {
        "history.task_failure_rate",
        "history.task_total_runs",
        "history.task_last_error",
        "history.task_error_groups",
        "history.trigger_fire_count",
        "history.trigger_last_value",
    }.issubset(sources)


# ---------------------------------------------------------------------------
# Task-history sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_failure_rate_and_total_runs_fire_threshold(_proactive, db_session):
    """failure_rate and total_runs reflect recorded runs; thresholds fire."""
    _, monitor = _proactive
    await record_task_run(db_session, task_id="t", status="completed", run_count=1)
    await record_task_run(db_session, task_id="t", status="completed", run_count=2)
    await record_task_run(db_session, task_id="t", status="failed", run_count=3, error="boom")
    await db_session.commit()

    low_rate = monitor.add_trigger(
        Trigger(
            name="half-broken",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="history.task_failure_rate:t",
                operator="gte",
                value=0.33,
            ),
        )
    )
    fired = await monitor.check_once()
    assert low_rate.id in fired

    runs = monitor.add_trigger(
        Trigger(
            name="three-runs",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="history.task_total_runs:t",
                operator="gte",
                value=3,
            ),
        )
    )
    fired2 = await monitor.check_once()
    assert runs.id in fired2


@pytest.mark.asyncio
async def test_task_failure_rate_quiet_when_low(_proactive, db_session):
    """A low failure rate does not fire a strict threshold."""
    _, monitor = _proactive
    await record_task_run(db_session, task_id="t", status="completed", run_count=1)
    await record_task_run(db_session, task_id="t", status="completed", run_count=2)
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="no-failures",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="history.task_failure_rate:t",
                operator="gt",
                value=0.5,
            ),
        )
    )
    assert await monitor.check_once() == []
    assert trigger.fire_count == 0


@pytest.mark.asyncio
async def test_task_last_error_fires_pattern(_proactive, db_session):
    """task_last_error exposes the latest failure message for pattern triggers."""
    _, monitor = _proactive
    await record_task_run(
        db_session, task_id="t", status="failed", run_count=1,
        error="retry exhausted",
    )
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="retry-alert",
            condition=TriggerCondition(
                condition_type=ConditionType.PATTERN,
                source="history.task_last_error:t",
                operator="contains",
                value="retry",
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired


@pytest.mark.asyncio
async def test_task_error_groups_counts_distinct(_proactive, db_session):
    """error_groups is the number of distinct failure messages."""
    _, monitor = _proactive
    await record_task_run(db_session, task_id="t", status="failed", run_count=1, error="timeout")
    await record_task_run(db_session, task_id="t", status="failed", run_count=2, error="timeout")
    await record_task_run(db_session, task_id="t", status="failed", run_count=3, error="refused")
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="two-kinds",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="history.task_error_groups:t",
                operator="gte",
                value=2,
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired


# ---------------------------------------------------------------------------
# Trigger-history sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_fire_count_and_last_value(_proactive, db_session):
    """fire_count counts firings; last_value exposes the most recent value."""
    _, monitor = _proactive
    await record_trigger_fire(
        db_session, trigger_id="tr", condition={"source": "x"}, value=5
    )
    await record_trigger_fire(
        db_session, trigger_id="tr", condition={"source": "x"}, value=9
    )
    await db_session.commit()

    # A positive cooldown keeps each trigger from re-firing on later ticks, so
    # the two sources are asserted independently and deterministically.
    count = monitor.add_trigger(
        Trigger(
            name="fired-twice",
            cooldown_seconds=3600,
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="history.trigger_fire_count:tr",
                operator="gte",
                value=2,
            ),
        )
    )
    fired = await monitor.check_once()
    assert count.id in fired

    last = monitor.add_trigger(
        Trigger(
            name="peak-value",
            cooldown_seconds=3600,
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="history.trigger_last_value:tr",
                operator="gte",
                value=7,
            ),
        )
    )
    fired2 = await monitor.check_once()
    assert last.id in fired2


# ---------------------------------------------------------------------------
# Reactor notifications
# ---------------------------------------------------------------------------


async def _list_inbox(*, reactor_only: bool = False):
    import backend.services.proactive_service as ps

    factory = ps._session_factory()
    async with factory() as session:
        items = await get_notification_service().list(session, limit=50)
    if reactor_only:
        items = [n for n in items if n.payload.get("origin") == "reactor"]
    return items


@pytest.mark.asyncio
async def test_reactor_listens_to_task_run(_proactive):
    """A reactor task listening to a source task emits a run-result notification."""
    scheduler, _ = _proactive
    source = scheduler.add_task(
        ScheduledTask(
            name="backup",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            payload={"title": "t", "message": "m"},
        )
    )
    reactor = scheduler.add_task(
        ScheduledTask(
            name="backup-reactor",
            handler="reactor",
            task_type=TaskType.ONE_SHOT,
            payload={"listen_task_id": source.id, "on": "all"},
        )
    )
    await scheduler.run_task(source.id)

    inbox = await _list_inbox(reactor_only=True)
    assert len(inbox) == 1
    record = inbox[0]
    assert "backup" in record.title
    assert record.payload["listened_task_id"] == source.id
    assert record.payload["status"] == "completed"
    assert record.payload["error_groups"] == 0
    assert reactor.id  # reactor task exists (id assigned)


@pytest.mark.asyncio
async def test_reactor_on_failure_only(_proactive):
    """A failure-only reactor stays quiet on success and reacts on failure."""
    scheduler, _ = _proactive
    source = scheduler.add_task(
        ScheduledTask(
            name="flaky",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            payload={"title": "t", "message": "m"},
        )
    )
    scheduler.add_task(
        ScheduledTask(
            name="flaky-reactor",
            handler="reactor",
            task_type=TaskType.ONE_SHOT,
            payload={"listen_task_id": source.id, "on": "failure"},
        )
    )
    await scheduler.run_task(source.id)
    assert await _list_inbox(reactor_only=True) == []

    async def _boom(_t):
        raise RuntimeError("explode")

    scheduler.register_handler("boom", _boom)
    bad = scheduler.add_task(
        ScheduledTask(name="bad", handler="boom", task_type=TaskType.ONE_SHOT)
    )
    scheduler.add_task(
        ScheduledTask(
            name="bad-reactor",
            handler="reactor",
            task_type=TaskType.ONE_SHOT,
            payload={"listen_task_id": bad.id, "on": "failure"},
        )
    )
    await scheduler.run_task(bad.id)
    assert bad.status == TaskStatus.FAILED

    inbox = await _list_inbox(reactor_only=True)
    assert len(inbox) == 1
    record = inbox[0]
    assert record.payload["status"] == "failed"
    assert record.payload["error"] == "explode"


@pytest.mark.asyncio
async def test_reactor_on_success_only(_proactive):
    """A success-only reactor reacts to a successful run and not a failure."""
    scheduler, _ = _proactive
    async def _boom(_t):
        raise RuntimeError("oops")

    scheduler.register_handler("boom", _boom)
    ok = scheduler.add_task(
        ScheduledTask(name="ok", handler="notify", task_type=TaskType.ONE_SHOT,
                      payload={"title": "t", "message": "m"})
    )
    scheduler.add_task(
        ScheduledTask(name="ok-reactor", handler="reactor", task_type=TaskType.ONE_SHOT,
                      payload={"listen_task_id": ok.id, "on": "success"})
    )
    await scheduler.run_task(ok.id)
    assert len(await _list_inbox(reactor_only=True)) == 1

    bad = scheduler.add_task(
        ScheduledTask(name="bad2", handler="boom", task_type=TaskType.ONE_SHOT)
    )
    scheduler.add_task(
        ScheduledTask(name="bad2-reactor", handler="reactor", task_type=TaskType.ONE_SHOT,
                      payload={"listen_task_id": bad.id, "on": "success"})
    )
    await scheduler.run_task(bad.id)
    # Only the success reactor from the successful task should have emitted.
    inbox = await _list_inbox(reactor_only=True)
    assert len(inbox) == 1


@pytest.mark.asyncio
async def test_reactor_standalone_run_is_noop(_proactive):
    """Running a reactor task on its own completes without emitting anything."""
    scheduler, _ = _proactive
    reactor = scheduler.add_task(
        ScheduledTask(
            name="lonely-reactor",
            handler="reactor",
            task_type=TaskType.ONE_SHOT,
            payload={"listen_task_id": "no-such-task", "on": "all"},
        )
    )
    await scheduler.run_task(reactor.id)
    assert reactor.status == TaskStatus.COMPLETED
    assert await _list_inbox() == []


@pytest.mark.asyncio
async def test_reactor_task_api_flow(client, _proactive):
    """The API path the frontend uses: create a reactor task, run a source,
    and see the reactor's run-result notification in the inbox."""
    # Source task (succeeds).
    src = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "api-source", "handler": "notify", "task_type": "one_shot",
              "payload": {"title": "t", "message": "m"}},
    )
    assert src.status_code == 201
    source_id = src.json()["id"]

    # Reactor task created with the same handler/payload shape the UI submits.
    reac = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "api-reactor", "handler": "reactor", "task_type": "one_shot",
              "payload": {"listen_task_id": source_id, "on": "all"}},
    )
    assert reac.status_code == 201
    assert reac.json()["handler"] == "reactor"
    assert (await client.get("/api/v1/scheduler/tasks")).status_code == 200

    # Run the source; its run hook streams a reactor notification.
    assert (await client.post(f"/api/v1/scheduler/tasks/{source_id}/run")).status_code == 200

    inbox = (await client.get("/api/v1/scheduler/notifications")).json()["notifications"]
    reactor_recs = [n for n in inbox if n.get("payload", {}).get("origin") == "reactor"]
    assert len(reactor_recs) == 1
    assert reactor_recs[0]["payload"]["listened_task_id"] == source_id
    assert reactor_recs[0]["payload"]["status"] == "completed"
