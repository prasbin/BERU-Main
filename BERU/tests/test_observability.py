"""Tests for the /metrics observability endpoint and request tracker."""

from __future__ import annotations

import pytest

from backend.services.observability import RequestMetricsTracker, reset_request_metrics

# ---- unit tests for the tracker / metric header contract ----


def test_tracker_rate_window_expires():
    tracker = RequestMetricsTracker()
    tracker.record(200)
    assert tracker.total == 1
    # Manually age the single entry beyond the window.
    tracker._recent[0] = (tracker._recent[0][0] - 61.0, 200)
    assert tracker.rate_per_1m() == 0.0
    assert tracker.snapshot()["total_requests"] == 1
    tracker.reset()
    assert tracker.total == 0


@pytest.mark.asyncio
async def test_error_responses_stamp_error_type_header(client):
    """Every BERU error envelope carries the X-Beru-Error-Type header."""
    missing = await client.get("/api/v1/plans/does-not-exist")
    assert missing.status_code == 404
    assert missing.headers.get("X-Beru-Error-Type") == "not_found"
    assert missing.json()["error"]["type"] == "not_found"


# ---- HTTP-level tests against the app ----


@pytest.mark.asyncio
async def test_metrics_endpoint_shape(client):
    resp = await client.get("/api/v1/metrics")
    assert resp.status_code == 200
    data = resp.json()

    requests = data["requests"]
    assert set(requests.keys()) == {
        "uptime_s",
        "total_requests",
        "requests_per_1m",
        "by_status",
        "by_error_type",
    }
    assert data["reliability"]["total_tool_calls"] >= 0
    assert set(data["proactive"].keys()) == {
        "scheduler_running",
        "monitor_running",
        "tasks",
        "triggers",
    }
    assert isinstance(data["active_sessions"], int)
    assert data["llm_provider"] == "mock"


@pytest.mark.asyncio
async def test_metrics_tracks_requests_and_error_envelopes(client):
    reset_request_metrics()

    ok = await client.get("/api/v1/status")
    assert ok.status_code == 200
    missing = await client.get("/api/v1/plans/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "not_found"

    data = (await client.get("/api/v1/metrics")).json()["requests"]
    # The /metrics call itself is recorded *after* the snapshot, so it is not
    # included in its own body.
    assert data["total_requests"] == 2
    assert data["by_status"] == {"200": 1, "404": 1}
    assert data["by_error_type"] == {"not_found": 1}
    assert data["requests_per_1m"] >= 2.0


@pytest.mark.asyncio
async def test_metrics_counts_own_request_after_response(client):
    reset_request_metrics()
    await client.get("/api/v1/status")
    await client.get("/api/v1/metrics")
    # The tracker has now recorded both calls (recorded after each response).
    from backend.services.observability import get_request_tracker

    snapshot = get_request_tracker().snapshot()
    assert snapshot["total_requests"] == 2
    assert snapshot["by_status"].get("200", 0) == 2