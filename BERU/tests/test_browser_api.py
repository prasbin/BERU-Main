"""Tests for the browser-automation API router.

These exercise the HTTP surface that powers the Browser UI panel. The engine
delegates to Playwright, so the browser boundary is mocked at the engine level
— the router is pointed at real ``BrowserPage``/``ActionResult`` objects so the
HTTP logic (envelopes, status codes, argument marshalling) is tested
deterministically without launching a real browser.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from backend.engines.browser import (
    ActionResult,
    BrowserPage,
    PageStatus,
    get_browser_engine,
)

# ---- availability ----


async def test_browser_status_reports_available(client):
    engine = get_browser_engine()
    info = {
        "available": True,
        "mode": "headless",
        "pages": 2,
        "active_page": "p1",
        "blocked_globs": 1,
        "redirects": 0,
        "started_at": "2026-01-01T00:00:00+00:00",
        "download_dir": "C:/tmp/dl",
        "session_dir": "C:/tmp/sess",
    }
    with patch.object(engine, "ensure_ready", AsyncMock(return_value=True)):
        with patch.object(engine, "status_info", return_value=info):
            resp = await client.get("/api/v1/browser/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["pages"] == 2
    assert body["mode"] == "headless"


async def test_browser_status_reports_unavailable(client):
    engine = get_browser_engine()
    with patch.object(engine, "ensure_ready", AsyncMock(return_value=True)):
        with patch.object(engine, "status_info",
                          return_value={"available": False, "mode": None,
                                        "pages": 0, "blocked_globs": 0,
                                        "redirects": 0}):
            resp = await client.get("/api/v1/browser/status")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


# ---- page endpoints ----


async def test_create_page_endpoint(client):
    engine = get_browser_engine()
    good = BrowserPage(id="abc123", url="https://example.com", title="Example")
    with patch.object(engine, "create_page", AsyncMock(return_value=good)):
        resp = await client.post("/api/v1/browser/pages", json={"url": "https://example.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["ok"] is True
    assert body["id"] == "abc123"
    assert body["url"] == "https://example.com"


async def test_create_page_error_sets_ok_false(client):
    engine = get_browser_engine()
    bad = BrowserPage(id="x1", url="about:blank", title="(error)")
    bad.status = PageStatus.ERROR
    with patch.object(engine, "create_page", AsyncMock(return_value=bad)):
        resp = await client.post("/api/v1/browser/pages", json={"url": "https://x.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "error"


async def test_list_pages_endpoint(client):
    engine = get_browser_engine()
    pages = [
        BrowserPage(id="a", url="https://a.com", title="A"),
        BrowserPage(id="b", url="https://b.com", title="B"),
    ]
    with patch.object(engine, "list_pages", return_value=pages):
        with patch.object(engine, "_active_page_id", "b", create=True):
            resp = await client.get("/api/v1/browser/pages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["pages"]) == 2
    assert body["active_page"] == "b"


async def test_get_page_endpoint(client):
    engine = get_browser_engine()
    with patch.object(engine, "get_page",
                      return_value=BrowserPage(id="a", url="https://a.com")):
        resp = await client.get("/api/v1/browser/pages/a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["id"] == "a"


async def test_get_page_not_found(client):
    engine = get_browser_engine()
    with patch.object(engine, "get_page", return_value=None):
        resp = await client.get("/api/v1/browser/pages/nope")
    assert resp.status_code == 404


async def test_activate_page_endpoint(client):
    engine = get_browser_engine()
    with patch.object(engine, "set_active_page", return_value=True):
        resp = await client.post("/api/v1/browser/pages/a/activate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["active_page"] == "a"


async def test_activate_page_not_found(client):
    engine = get_browser_engine()
    with patch.object(engine, "set_active_page", return_value=False):
        resp = await client.post("/api/v1/browser/pages/nope/activate")
    assert resp.status_code == 404


async def test_close_page_endpoint(client):
    engine = get_browser_engine()
    with patch.object(engine, "close_page", return_value=True):
        resp = await client.delete("/api/v1/browser/pages/a")
    assert resp.status_code == 204


async def test_close_page_not_found(client):
    engine = get_browser_engine()
    with patch.object(engine, "close_page", return_value=False):
        resp = await client.delete("/api/v1/browser/pages/nope")
    assert resp.status_code == 404


# ---- action endpoint ----


async def test_action_endpoint_navigate(client):
    engine = get_browser_engine()
    ok = ActionResult(
        action="navigate",
        data={"url": "https://x.com", "title": "X"},
    )
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)):
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "navigate", "url": "https://x.com"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "navigate"
    assert body["data"]["url"] == "https://x.com"


async def test_action_endpoint_failure(client):
    engine = get_browser_engine()
    failed = ActionResult(
        success=False, action="navigate", error="No active page"
    )
    with patch.object(engine, "execute_action", AsyncMock(return_value=failed)):
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "navigate", "url": "https://x.com"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["success"] is False
    assert "No active page" in body["error"]


async def test_action_endpoint_unknown_action(client):
    resp = await client.post("/api/v1/browser/action", json={"action": "bogus"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "http_error"
    assert "Unknown action" in body["error"]["message"]


async def test_action_endpoint_passes_all_kwargs(client):
    engine = get_browser_engine()
    ok = ActionResult(action="click", data={})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        await client.post(
            "/api/v1/browser/action",
            json={
                "action": "click",
                "selector": "#btn",
                "url": "https://a.com",
                "text": "hey",
                "direction": "down",
                "amount": 100,
                "seconds": 2.5,
                "key": "Enter",
                "block": "start",
                "value": "CA",
                "label": "Canada",
                "index": 3,
                "values": ["a", "b"],
                "full_page": True,
                "frame": "editor",
                "source": "#item",
                "target": "#zone",
                "subcommand": "set",
                "cookies": [{"name": "x", "value": "1"}],
                "glob": "*ads*",
                "paths": ["/tmp/a.txt"],
                "limit": 10,
                "name": "my_session",
                "redirect": "https://new.com",
                "body": "{}",
                "content_type": "application/json",
                "status_code": 201,
                "headless": False,
                "width": 1000,
                "height": 700,
                "user_agent": "TestUA",
                "locale": "en-US",
                "headers": {"X-Rate-Limit": "100"},
                "device_scale_factor": 3,
                "is_mobile": True,
                "has_touch": True,
                "timezone_id": "Asia/Kolkata",
            },
        )
    mock.assert_awaited_once()
    kwargs = mock.call_args.kwargs
    assert kwargs["selector"] == "#btn"
    assert kwargs["url"] == "https://a.com"
    assert kwargs["text"] == "hey"
    assert kwargs["direction"] == "down"
    assert kwargs["amount"] == 100
    assert kwargs["seconds"] == 2.5
    assert kwargs["key"] == "Enter"
    assert kwargs["block"] == "start"
    assert kwargs["value"] == "CA"
    assert kwargs["label"] == "Canada"
    assert kwargs["index"] == 3
    assert kwargs["values"] == ["a", "b"]
    assert kwargs["full_page"] is True
    assert kwargs["frame"] == "editor"
    assert kwargs["source"] == "#item"
    assert kwargs["target"] == "#zone"
    assert kwargs["subcommand"] == "set"
    assert kwargs["cookies"] == [{"name": "x", "value": "1"}]
    assert kwargs["glob"] == "*ads*"
    assert kwargs["paths"] == ["/tmp/a.txt"]
    assert kwargs["limit"] == 10
    assert kwargs["name"] == "my_session"
    assert kwargs["redirect"] == "https://new.com"
    assert kwargs["body"] == "{}"
    assert kwargs["content_type"] == "application/json"
    assert kwargs["status_code"] == 201
    assert kwargs["headless"] is False
    assert kwargs["width"] == 1000
    assert kwargs["height"] == 700
    assert kwargs["user_agent"] == "TestUA"
    assert kwargs["locale"] == "en-US"
    assert kwargs["headers"] == {"X-Rate-Limit": "100"}
    assert kwargs["device_scale_factor"] == 3
    assert kwargs["is_mobile"] is True
    assert kwargs["has_touch"] is True
    assert kwargs["timezone_id"] == "Asia/Kolkata"


async def test_action_endpoint_page_info(client):
    engine = get_browser_engine()
    ok = ActionResult(action="page_info", data={"url": "https://x.com", "title": "X"})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)):
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "page_info"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["title"] == "X"


async def test_action_endpoint_press(client):
    engine = get_browser_engine()
    ok = ActionResult(action="press", data={"key": "Enter"})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)):
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "press", "key": "Enter"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["key"] == "Enter"


async def test_action_endpoint_select(client):
    engine = get_browser_engine()
    ok = ActionResult(action="select", data={"selected": True, "value": "CA"})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "select", "selector": "#country", "value": "CA"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["selected"] is True
    kwargs = mock.call_args.kwargs
    assert kwargs["selector"] == "#country"
    assert kwargs["value"] == "CA"


async def test_action_endpoint_select_label_index(client):
    engine = get_browser_engine()
    ok = ActionResult(action="select", data={"selected": True})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "select", "selector": "#plan", "label": "Pro", "index": 1},
        )
    assert resp.status_code == 200
    kwargs = mock.call_args.kwargs
    assert kwargs["label"] == "Pro"
    assert kwargs["index"] == 1


async def test_action_endpoint_screenshot_full_page(client):
    engine = get_browser_engine()
    ok = ActionResult(action="screenshot", data={"full_page": True})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "screenshot", "full_page": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["full_page"] is True
    assert mock.call_args.kwargs.get("full_page") is True


async def test_action_endpoint_screenshot_viewport(client):
    engine = get_browser_engine()
    ok = ActionResult(action="screenshot", data={"full_page": False})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "screenshot"}
        )
    assert resp.status_code == 200
    assert "full_page" not in mock.call_args.kwargs


async def test_action_endpoint_drag(client):
    engine = get_browser_engine()
    ok = ActionResult(action="drag", data={"dragged": True})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "drag", "source": "#a", "target": "#b"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    kwargs = mock.call_args.kwargs
    assert kwargs["source"] == "#a"
    assert kwargs["target"] == "#b"


async def test_action_endpoint_cookies(client):
    engine = get_browser_engine()
    ok = ActionResult(action="cookies", data={"count": 1, "cleared": False})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "cookies", "subcommand": "clear"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["cleared"] is False
    assert mock.call_args.kwargs["subcommand"] == "clear"


async def test_action_endpoint_storage(client):
    engine = get_browser_engine()
    ok = ActionResult(action="storage", data={"key": "theme", "set": True})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "storage", "subcommand": "set", "key": "theme", "value": "dark"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["set"] is True
    assert mock.call_args.kwargs["key"] == "theme"
    assert mock.call_args.kwargs["value"] == "dark"


async def test_action_endpoint_network(client):
    engine = get_browser_engine()
    ok = ActionResult(action="network", data={"count": 3})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "network", "limit": 3}
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 3
    assert mock.call_args.kwargs["limit"] == 3


async def test_action_endpoint_block_url(client):
    engine = get_browser_engine()
    ok = ActionResult(action="block_url", data={"glob": "*ads*"})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "block_url", "glob": "*ads*"}
        )
    assert resp.status_code == 200
    assert mock.call_args.kwargs["glob"] == "*ads*"


async def test_action_endpoint_upload(client):
    engine = get_browser_engine()
    ok = ActionResult(action="upload", data={"uploaded": True})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "upload", "selector": "#f", "paths": ["/tmp/a.txt"]},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["uploaded"] is True
    assert mock.call_args.kwargs["paths"] == ["/tmp/a.txt"]


async def test_action_endpoint_download(client):
    engine = get_browser_engine()
    ok = ActionResult(action="download", data={"filename": "report.pdf"})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "download", "selector": "#dl"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["filename"] == "report.pdf"
    assert mock.call_args.kwargs["selector"] == "#dl"


async def test_action_endpoint_frame(client):
    engine = get_browser_engine()
    ok = ActionResult(action="click", data={"clicked": True})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "click", "selector": "#b", "frame": "editor"},
        )
    assert resp.status_code == 200
    assert mock.call_args.kwargs["frame"] == "editor"


async def test_action_endpoint_console(client):
    engine = get_browser_engine()
    ok = ActionResult(action="console", data={"count": 2, "entries": []})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)):
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "console"}
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 2


async def test_action_endpoint_downloads(client):
    engine = get_browser_engine()
    ok = ActionResult(action="downloads", data={"count": 1, "files": []})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "downloads", "limit": 5}
        )
    assert resp.status_code == 200
    assert mock.call_args.kwargs["limit"] == 5
    assert resp.json()["data"]["count"] == 1


async def test_action_endpoint_redirect_url(client):
    engine = get_browser_engine()
    ok = ActionResult(action="redirect_url", data={"glob": "*old*", "redirect": "https://new.com"})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={"action": "redirect_url", "glob": "*old*", "redirect": "https://new.com"},
        )
    assert resp.status_code == 200
    kwargs = mock.call_args.kwargs
    assert kwargs["glob"] == "*old*"
    assert kwargs["redirect"] == "https://new.com"


async def test_action_endpoint_snapshot(client):
    engine = get_browser_engine()
    ok = ActionResult(action="snapshot", data={"name": "alpha", "cookies": 2, "origins": 1})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "snapshot", "name": "alpha"}
        )
    assert resp.status_code == 200
    assert mock.call_args.kwargs["name"] == "alpha"
    assert resp.json()["data"]["cookies"] == 2


async def test_action_endpoint_restore(client):
    engine = get_browser_engine()
    ok = ActionResult(action="restore", data={"restored": True, "name": "alpha"})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "restore", "name": "alpha"}
        )
    assert resp.status_code == 200
    assert mock.call_args.kwargs["name"] == "alpha"
    assert resp.json()["data"]["restored"] is True


async def test_action_endpoint_fulfill(client):
    engine = get_browser_engine()
    ok = ActionResult(action="fulfill", data={"glob": "*/api/*", "status": 201})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={
                "action": "fulfill",
                "glob": "*/api/*",
                "body": "{}",
                "content_type": "application/json",
                "status_code": 201,
                "headers": {"X-Rate-Limit": "100"},
            },
        )
    assert resp.status_code == 200
    kwargs = mock.call_args.kwargs
    assert kwargs["glob"] == "*/api/*"
    assert kwargs["body"] == "{}"
    assert kwargs["content_type"] == "application/json"
    assert kwargs["status_code"] == 201
    assert kwargs["headers"] == {"X-Rate-Limit": "100"}
    assert resp.json()["data"]["status"] == 201


async def test_action_endpoint_clear_downloads(client):
    engine = get_browser_engine()
    ok = ActionResult(action="clear_downloads", data={"removed": 2, "failed": 0})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action", json={"action": "clear_downloads"}
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["data"]["removed"] == 2
    assert mock.await_count == 1


async def test_action_endpoint_launch(client):
    engine = get_browser_engine()
    ok = ActionResult(action="launch", data={"launched": True, "headless": False})
    with patch.object(engine, "execute_action", AsyncMock(return_value=ok)) as mock:
        resp = await client.post(
            "/api/v1/browser/action",
            json={
                "action": "launch",
                "headless": False,
                "width": 1000,
                "height": 700,
                "device_scale_factor": 3,
                "is_mobile": True,
                "has_touch": True,
                "timezone_id": "Asia/Kolkata",
            },
        )
    assert resp.status_code == 200
    kwargs = mock.call_args.kwargs
    assert kwargs["headless"] is False
    assert kwargs["width"] == 1000
    assert kwargs["height"] == 700
    assert kwargs["device_scale_factor"] == 3
    assert kwargs["is_mobile"] is True
    assert kwargs["has_touch"] is True
    assert kwargs["timezone_id"] == "Asia/Kolkata"
    assert resp.json()["data"]["launched"] is True


# ---- close-browser endpoint ----


async def test_close_browser_endpoint(client):
    engine = get_browser_engine()
    with patch.object(engine, "close_browser", AsyncMock(return_value=True)):
        resp = await client.post("/api/v1/browser/close")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["closed"] is True
    assert body["pages"] == 0


async def test_close_browser_endpoint_idempotent(client):
    engine = get_browser_engine()
    with patch.object(engine, "close_browser", AsyncMock(return_value=False)):
        resp = await client.post("/api/v1/browser/close")
    assert resp.status_code == 200
    assert resp.json()["closed"] is False
