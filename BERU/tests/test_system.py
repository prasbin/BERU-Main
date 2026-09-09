"""Tests for system endpoints: health, root, status, discovery."""

from __future__ import annotations


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "BERU"


async def test_root(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    # Root serves frontend HTML or JSON depending on frontend availability
    content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        assert "BERU" in resp.text
    else:
        assert resp.json()["app"] == "BERU"


async def test_status_reports_mock_provider(client):
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    assert resp.json()["llm_provider"] == "mock"


async def test_request_id_header_present(client):
    resp = await client.get("/health")
    header_keys = {k.lower() for k in resp.headers}
    assert "x-request-id" in header_keys


async def test_list_agents(client):
    resp = await client.get("/api/v1/agents")
    assert resp.status_code == 200
    assert "beru_core" in [a["name"] for a in resp.json()]


async def test_list_tools(client):
    resp = await client.get("/api/v1/tools")
    assert resp.status_code == 200
    assert "clock" in [t["name"] for t in resp.json()]
