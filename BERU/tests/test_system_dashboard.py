"""Backend contract tests for the Interactive Assistant UI dashboard endpoints.

Covers:
  * WS3: LLM status and connection-test endpoints
  * WS5: agent-aggregated tool list with availability
  * WS4: scheduler CRUD, run-history/error-groups, and monitor status endpoints
"""

from __future__ import annotations

import pytest

# ---- WS3: LLM status & test ----


@pytest.mark.asyncio
async def test_llm_info_returns_configuration(client):
    resp = await client.get("/api/v1/llm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "mock"
    assert body["configured"] is False
    assert body["model"]
    assert "base_url" in body
    assert isinstance(body["has_api_key"], bool)
    # last_test may or may not be present depending on execution order,
    # but when it is present it must have the right shape.
    if body["last_test"] is not None:
        assert body["last_test"]["ok"] in (True, False)
        assert "provider" in body["last_test"]


@pytest.mark.asyncio
async def test_llm_test_mock_reported_not_connected(client):
    resp = await client.post("/api/v1/llm/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["connected"] is False
    assert body["provider"] == "mock"
    assert body["latency_ms"] is not None
    assert body["note"] is not None and "mock" in body["note"].lower()

    # The last_test is now persisted so the GET endpoint surfaces it.
    get_resp = await client.get("/api/v1/llm")
    assert get_resp.status_code == 200
    last = get_resp.json()["last_test"]
    assert last is not None
    assert last["ok"] is True
    assert last["connected"] is False


# ---- WS5: aggregated tool list + availability ----


@pytest.mark.asyncio
async def test_tools_aggregates_across_agents(client):
    resp = await client.get("/api/v1/tools")
    assert resp.status_code == 200
    tools = resp.json()
    names = [t["name"] for t in tools]
    # Tools from multiple agents should be present.
    assert "clock" in names
    assert "run_command" in names
    assert "web_search" in names
    assert "browser_navigate" in names
    assert "browser_select" in names
    assert "browser_close" in names
    assert "browser_drag" in names
    assert "browser_cookies" in names
    assert "browser_storage" in names
    assert "browser_network" in names
    assert "browser_block_url" in names
    assert "browser_download" in names
    assert "browser_upload" in names
    assert "browser_console" in names
    assert "browser_downloads" in names
    assert "browser_redirect_url" in names
    assert "browser_snapshot" in names
    assert "browser_restore" in names
    assert "browser_fulfill" in names
    assert "browser_launch" in names
    assert "browser_clear_downloads" in names
    assert "voice_speak" in names
    assert "code_analyser" in names
    # Availability is honest.
    by_name = {t["name"]: t for t in tools}
    assert by_name["run_command"]["availability"] == "available"
    assert by_name["code_analyser"]["availability"] == "limited"
    assert by_name["web_search"]["availability"] == "unavailable"
    assert by_name["browser_navigate"]["availability"] == "available"
    assert by_name["browser_select"]["availability"] == "available"
    assert by_name["browser_close"]["availability"] == "available"
    assert by_name["browser_network"]["availability"] == "available"
    assert by_name["browser_console"]["availability"] == "available"
    assert by_name["browser_restore"]["availability"] == "available"
    assert by_name["browser_fulfill"]["availability"] == "available"
    assert by_name["browser_launch"]["requires_confirmation"] is True
    assert by_name["browser_clear_downloads"]["availability"] == "available"
    assert by_name["browser_clear_downloads"]["requires_confirmation"] is True
    assert by_name["browser_close"]["requires_confirmation"] is True
    assert by_name["voice_speak"]["availability"] == "unavailable"
    assert by_name["voice_speak"]["requires_confirmation"] is True


# ---- WS4: scheduler dashboard smoke ----


@pytest.mark.asyncio
async def test_scheduler_create_list_run_history(client, _sessionmaker):
    create = await client.post(
        "/api/v1/scheduler/tasks",
        json={
            "name": "ui-test-task",
            "handler": "notify",
            "task_type": "interval",
            "interval_seconds": 999,
            "payload": {"title": "t", "message": "m"},
        },
    )
    assert create.status_code == 201
    task_id = create.json()["id"]
    assert create.json()["name"] == "ui-test-task"

    # List contains the task.
    listing = await client.get("/api/v1/scheduler/tasks")
    assert any(t["id"] == task_id for t in listing.json())

    # Run now.
    run = await client.post(f"/api/v1/scheduler/tasks/{task_id}/run")
    assert run.status_code == 200
    assert run.json()["id"] == task_id

    # Pause/resume/cancel.
    pause = await client.post(f"/api/v1/scheduler/tasks/{task_id}/pause")
    assert pause.status_code == 200
    assert pause.json()["status"] == "paused"
    resume = await client.post(f"/api/v1/scheduler/tasks/{task_id}/resume")
    assert resume.status_code == 200
    # Resuming re-arms the next run, so the task is pending (not paused).
    assert resume.json()["status"] == "pending"
    cancel = await client.post(f"/api/v1/scheduler/tasks/{task_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"

    # Run history: at least one row from the run-now call.
    history = await client.get(f"/api/v1/scheduler/tasks/{task_id}/runs")
    assert history.status_code == 200
    h = history.json()
    assert h["task_id"] == task_id
    assert h["total"] >= 1
    assert isinstance(h["summary"], dict)
    assert isinstance(h["summary"]["error_groups"], list)
    assert isinstance(h["runs"], list)

    # Delete.
    delete = await client.delete(f"/api/v1/scheduler/tasks/{task_id}")
    assert delete.status_code == 204


@pytest.mark.asyncio
async def test_monitor_status_sources_tick(client):
    status = await client.get("/api/v1/monitor/status")
    assert status.status_code == 200
    body = status.json()
    assert isinstance(body["running"], bool)
    assert isinstance(body["sources"], list)
    assert isinstance(body["triggers"], int)

    sources = await client.get("/api/v1/monitor/sources")
    assert sources.status_code == 200
    assert isinstance(sources.json()["sources"], list)

    tick = await client.post("/api/v1/monitor/tick")
    assert tick.status_code == 200
    assert "fired" in tick.json()
