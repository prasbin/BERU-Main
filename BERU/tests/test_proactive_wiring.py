"""Wiring tests for the proactive/autonomous layer.

Covers the shared runtime (scheduler + monitor + notifications) wired into the
app: handlers that persist notifications and run real agent turns, monitor
triggers that fire against real sources, the scheduler/monitor routers, and the
lifespan start/stop of both loops.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.core.config import get_settings
from backend.database.base import get_session
from backend.engines.llm.registry import reset_llm_provider
from backend.engines.monitor import ConditionType, Trigger, TriggerCondition, TriggerStatus
from backend.engines.scheduler import ScheduledTask, TaskType
from backend.main import create_app
from backend.models.conversation import Conversation
from backend.services.proactive_service import (
    get_event_monitor,
    get_notification_service,
    get_scheduler,
    set_session_factory,
)


@pytest_asyncio.fixture
async def _proactive(_sessionmaker):
    """Point the shared proactive runtime at the temp DB and clear its state."""
    set_session_factory(lambda: _sessionmaker)
    get_scheduler().clear()
    get_event_monitor().clear()
    return get_scheduler(), get_event_monitor()


async def _list_notifications(session, title_prefix: str | None = None):
    records = await get_notification_service().list(session, limit=100)
    return [r for r in records if not title_prefix or r.title.startswith(title_prefix)]


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_notify_handler_persists_notification(_proactive, db_session):
    """A 'notify' task sends a notification persisted into the database."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="reminder",
            description="Test",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            payload={
                "title": "Stand up reminder",
                "message": "Time to stretch for a minute.",
                "level": "warning",
            },
        )
    )

    await scheduler.run_task(task.id)

    assert task.status.value == "completed"
    assert task.run_count == 1

    records = await _list_notifications(db_session, title_prefix="Stand up reminder")
    assert len(records) == 1
    assert records[0].level == "warning"
    assert records[0].channel == "in_app"
    assert records[0].read is False


@pytest.mark.asyncio
async def test_scheduler_agent_turn_handler_runs_real_turn(_proactive, db_session):
    """An 'agent_turn' task executes a real agent conversation through ChatService."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="morning-brief",
            description="Scheduled agent turn",
            handler="agent_turn",
            task_type=TaskType.ONE_SHOT,
            agent="beru_core",
            payload={"message": "Give me a one line status update."},
        )
    )

    await scheduler.run_task(task.id)

    assert task.status.value == "completed"
    assert task.last_error is None

    conversations = (
        await db_session.execute(
            select(Conversation).where(Conversation.agent == "beru_core")
        )
    ).scalars().all()
    assert len(conversations) == 1

    completions = await _list_notifications(db_session, title_prefix="Scheduled turn completed")
    assert len(completions) >= 1
    first = completions[0]
    assert first.payload.get("conversation_id") == conversations[0].id


@pytest.mark.asyncio
async def test_scheduler_run_due_executes_due_tasks(_proactive, db_session):
    """run_due() executes interval tasks whose next_run has arrived and reschedules."""
    scheduler, _ = _proactive
    task = scheduler.add_task(
        ScheduledTask(
            name="every-second",
            description="Test",
            handler="notify",
            task_type=TaskType.INTERVAL,
            interval_seconds=1,
            max_runs=1,
            next_run=datetime.now(timezone.utc),
            payload={"title": "tick", "message": "tick tock"},
        )
    )

    ran = await scheduler.run_due()
    assert [t.id for t in ran] == [task.id]
    assert task.run_count == 1
    assert task.max_runs == 1 and task.status.value == "completed"


# ---------------------------------------------------------------------------
# Monitor wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_trigger_fires_persists_alert_and_runs_action(_proactive, db_session):
    """A threshold breach fires the trigger: alert persisted and action dispatched."""
    scheduler, monitor = _proactive
    trigger = monitor.add_trigger(
        Trigger(
            name="high-unread",
            description="Too many unread notifications",
            status=TriggerStatus.ACTIVE,
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="notifications.unread",
                operator="gte",
                value=1,
            ),
            actions=[
                {
                    "type": "notify",
                    "title": "Inbox overload",
                    "message": "You have unread items.",
                }
            ],
        )
    )

    # Create one unread notification so the unread source reports >= 1.
    service = get_notification_service()
    await service.create(
        db_session, title="first", message="one", level="info", channel="in_app", payload={}
    )
    await db_session.commit()

    fired = await monitor.check_once()
    assert trigger.id in fired
    assert trigger.fire_count == 1

    # Alert from the sink.
    alerts = await _list_notifications(db_session, title_prefix="Trigger fired")
    assert len(alerts) == 1
    assert alerts[0].message == f"Condition met on source 'notifications.unread': gte {1}"

    # Action from the action runner.
    actions = await _list_notifications(db_session, title_prefix="Inbox overload")
    assert len(actions) == 1

    # Cooldown zero -> re-armed, next pass fires again.
    fired2 = await monitor.check_once()
    assert trigger.id in fired2
    assert trigger.fire_count == 2


@pytest.mark.asyncio
async def test_monitor_cooldown_suppresses_refire(_proactive, db_session):
    """A trigger in cooldown does not re-fire until the cooldown elapses."""
    scheduler, monitor = _proactive
    trigger = monitor.add_trigger(
        Trigger(
            name="cooldown-guard",
            description="Test cooldown",
            status=TriggerStatus.ACTIVE,
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="custom.load",
                operator="gt",
                value=5,
            ),
            cooldown_seconds=60,
        )
    )
    # 'custom.load' is not a registered source, so check_once() won't overwrite it.
    monitor.update_source_value("custom.load", 10)

    fired = await monitor.check_once()
    assert trigger.id in fired
    assert trigger.fire_count == 1

    # Immediately after firing the trigger is in cooldown -> no second fire.
    fired2 = await monitor.check_once()
    assert trigger.id not in fired2
    assert trigger.fire_count == 1
    assert trigger.status.value == "fired"


# ---------------------------------------------------------------------------
# Scheduler API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_api_crud_and_run(client):
    """Task create/list/pause/resume/cancel/run/delete over the HTTP API."""
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "api-task",
            "handler": "notify",
            "task_type": "interval",
            "interval_seconds": 3600,
            "payload": {"title": "hi", "message": "hello"},
        },
    )
    assert resp.status_code == 201
    task = resp.json()
    assert task["handler"] == "notify"
    assert task["status"] == "pending"

    listing = (await client.get("/api/v1/scheduler/tasks")).json()
    assert [t["id"] for t in listing] == [task["id"]]

    await client.post(f"/api/v1/scheduler/tasks/{task['id']}/pause")
    listing = (await client.get("/api/v1/scheduler/tasks")).json()
    assert listing[0]["status"] == "paused"

    await client.post(f"/api/v1/scheduler/tasks/{task['id']}/resume")
    assert (await client.get(f"/api/v1/scheduler/tasks/{task['id']}")).json()["status"] == "pending"

    run_resp = await client.post(f"/api/v1/scheduler/tasks/{task['id']}/run")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] in ("completed", "pending")

    await client.post(f"/api/v1/scheduler/tasks/{task['id']}/cancel")
    listing = (await client.get("/api/v1/scheduler/tasks")).json()
    assert listing[0]["status"] == "cancelled"

    assert (
        await client.delete(f"/api/v1/scheduler/tasks/{task['id']}")
    ).status_code == 204
    assert (await client.get(f"/api/v1/scheduler/tasks/{task['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_scheduler_api_rejects_unknown_handler(client):
    resp = await client.post(
        "/api/v1/scheduler/tasks",
        json={"name": "bad", "handler": "explode"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Notification API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_api_crud(client):
    """Create/list/mark-read/delete/clear notifications over the HTTP API."""
    resp = await client.post(
        "/api/v1/scheduler/notifications",
        json={
            "title": "API notification",
            "message": "Hello",
            "level": "warning",
            "channel": "in_app",
        },
    )
    assert resp.status_code == 201
    record = resp.json()
    assert record["level"] == "warning"
    assert record["read"] is False

    data = (await client.get("/api/v1/scheduler/notifications")).json()
    assert data["unread_count"] == 1
    assert data["total"] == 1

    unread = (await client.get("/api/v1/scheduler/notifications/unread-count")).json()
    assert unread["unread_count"] == 1

    await client.post(f"/api/v1/scheduler/notifications/{record['id']}/read")
    unread = await client.get("/api/v1/scheduler/notifications/unread-count")
    assert unread.json()["unread_count"] == 0

    await client.post("/api/v1/scheduler/notifications/read-all")
    unread = await client.get("/api/v1/scheduler/notifications/unread-count")
    assert unread.json()["unread_count"] == 0

    assert (
        await client.delete(f"/api/v1/scheduler/notifications/{record['id']}")
    ).status_code == 204
    assert (await client.get("/api/v1/scheduler/notifications")).json()["total"] == 0


# ---------------------------------------------------------------------------
# Monitor API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_api_tick_detects_bad_value(client, _proactive):
    """Create a trigger, push a bad source value, and tick fires it."""
    resp = await client.post(
        "/api/v1/monitor/triggers",
        json={
            "name": "high-temperature",
            "condition_type": "threshold",
            "source": "reading.temperature",
            "operator": "gt",
            "value": 80,
        },
    )
    assert resp.status_code == 201
    trigger_id = resp.json()["id"]

    assert (await client.get("/api/v1/monitor/status")).json()["running"] is False
    sources = (await client.get("/api/v1/monitor/sources")).json()["sources"]
    assert "notifications.unread" in sources

    pushed = await client.post(
        "/api/v1/monitor/sources/reading.temperature", json={"value": 95}
    )
    assert pushed.status_code == 200

    tick = await client.post("/api/v1/monitor/tick")
    assert tick.status_code == 200
    assert tick.json()["fired"] == [trigger_id]

    trigger = (await client.get(f"/api/v1/monitor/triggers/{trigger_id}")).json()
    assert trigger["fire_count"] == 1

    # Add trigger lifecycle checks: pause disables firing.
    await client.post(f"/api/v1/monitor/triggers/{trigger_id}/pause")
    tick2 = await client.post("/api/v1/monitor/tick")
    assert tick2.json()["fired"] == []
    assert (
        await client.delete(f"/api/v1/monitor/triggers/{trigger_id}")
    ).status_code == 204


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


TEST_API_KEY = "proactive-test-key"


@pytest_asyncio.fixture
async def auth_client(_sessionmaker, monkeypatch):
    """Client with API-key auth enabled."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("BERU_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("HOST", "127.0.0.1")
    get_settings.cache_clear()
    reset_llm_provider()
    set_session_factory(lambda: _sessionmaker)

    async def override_get_session():
        async with _sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    reset_llm_provider()


@pytest.mark.asyncio
async def test_scheduler_router_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/scheduler/tasks")
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"
    ok = await auth_client.get(
        "/api/v1/scheduler/tasks", headers={"X-API-Key": TEST_API_KEY}
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_monitor_router_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/monitor/sources")
    assert resp.status_code == 401
    ok = await auth_client.get(
        "/api/v1/monitor/sources", headers={"X-API-Key": TEST_API_KEY}
    )
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# Lifespan wiring (scheduler + monitor start/stop with the app)
# ---------------------------------------------------------------------------

# NOTE: exercising the full FastAPI lifespan via TestClient on this Windows
# box triggers a pre-existing exit-time access violation (anyio portal join +
# aiosqlite on Python 3.12), unrelated to this feature. The lifecycle is
# therefore covered here by driving the runtime directly, exactly as the app
# lifespan does; route registration is asserted separately (no lifespan).


@pytest.mark.asyncio
async def test_proactive_runtime_start_and_stop(_proactive):
    """start_proactive_runtime()/stop_proactive_runtime() mirror the lifespan."""
    from backend.services.proactive_service import (
        start_proactive_runtime,
        stop_proactive_runtime,
    )

    scheduler, monitor = _proactive

    await start_proactive_runtime()
    assert scheduler.running is True
    assert monitor.running is True

    # A task created while the loop runs is executed in the background.
    task = scheduler.add_task(
        ScheduledTask(
            name="lifespan-notify",
            description="Runs under the started loop",
            handler="notify",
            task_type=TaskType.ONE_SHOT,
            payload={"title": "lifespan", "message": "ran"},
        )
    )
    task.next_run = datetime.now(timezone.utc)  # force immediate due

    for _ in range(50):
        if task.run_count == 1:
            break
        await asyncio.sleep(0.1)
    assert task.run_count == 1

    await stop_proactive_runtime()
    assert scheduler.running is False
    assert monitor.running is False


def test_app_registers_scheduler_and_monitor_routes():
    """The app exposes the scheduler + monitor routers over /api/v1."""
    from backend.main import create_app

    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/v1/scheduler/tasks" in paths
    assert "/api/v1/scheduler/notifications" in paths
    assert "/api/v1/monitor/status" in paths
    assert "/api/v1/monitor/tick" in paths
    assert "/api/v1/monitor/triggers" in paths