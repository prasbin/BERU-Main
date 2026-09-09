"""Tests for the desktop-control API router.

These exercise the HTTP surface that powers the Desktop UI panel. The underlying
engines perform real host actions, so the genuinely-invasive calls (mouse
click/move, keyboard typing) and environment-dependent reads are mocked at the
engine boundary so the router logic is tested deterministically without moving
the user's real cursor.
"""

from __future__ import annotations

from unittest.mock import patch

from backend.engines.apps import get_app_inspect_engine
from backend.engines.clipboard import get_clipboard_engine
from backend.engines.hotkeys import get_hotkey_engine
from backend.engines.input import get_keyboard_engine, get_mouse_engine
from backend.engines.screenshot import get_screenshot_engine
from backend.engines.windows import get_windows_control_engine


def _ok(payload: dict):
    """Return a fake engine result whose to_dict() yields ``payload``."""
    return type("R", (), {"error": None, "to_dict": lambda s, p=payload: p})()


def _unavailable(message: str):
    return type("R", (), {"error": message, "to_dict": lambda s: {}})()


def _patch(engine_getter, method, ret):
    return patch.object(engine_getter(), method, return_value=ret)


# ---- availability ----


async def test_desktop_status_reports_capabilities(client):
    resp = await client.get("/api/v1/desktop/status")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("screenshot", "mouse", "keyboard", "clipboard", "apps", "screen_size",
                "windows", "monitors", "hotkeys"):
        assert key in body
        assert isinstance(body[key], bool)


# ---- screenshot ----


async def test_screenshot_capture_success(client):
    payload = _ok({"path": "/tmp/x.png", "width": 800, "height": 600})
    with _patch(get_screenshot_engine, "capture", payload):
        resp = await client.post("/api/v1/desktop/screenshot")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["path"] == "/tmp/x.png"


async def test_screenshot_capture_unavailable(client):
    with _patch(get_screenshot_engine, "capture", _unavailable("no backend")):
        resp = await client.post("/api/v1/desktop/screenshot")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["error"] == "no backend"


# ---- mouse ----


async def test_mouse_move_endpoint(client):
    with _patch(get_mouse_engine, "move", _ok({"x": 5, "y": 6})) as mv:
        resp = await client.post("/api/v1/desktop/mouse/move", json={"x": 5, "y": 6})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mv.assert_called_once_with(5, 6, duration=0.0)


async def test_mouse_click_endpoint(client):
    with _patch(get_mouse_engine, "click", _ok({"button": "left"})) as cl:
        resp = await client.post("/api/v1/desktop/mouse/click", json={"x": 1, "y": 2})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    cl.assert_called_once_with(x=1, y=2, button="left")


async def test_mouse_double_click_endpoint(client):
    with _patch(get_mouse_engine, "double_click", _ok({"button": "left"})):
        resp = await client.post("/api/v1/desktop/mouse/double-click", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_mouse_scroll_endpoint(client):
    with _patch(get_mouse_engine, "scroll", _ok({"amount": -3})) as sc:
        resp = await client.post("/api/v1/desktop/mouse/scroll", json={"amount": -3})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    sc.assert_called_once_with(-3, x=None, y=None)


async def test_mouse_position_endpoint(client):
    with _patch(get_mouse_engine, "position", _ok({"x": 10, "y": 20})):
        resp = await client.get("/api/v1/desktop/mouse/position")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["x"] == 10


async def test_mouse_screen_size_endpoint(client):
    with _patch(get_mouse_engine, "screen_size", _ok({"width": 1920, "height": 1080})):
        resp = await client.get("/api/v1/desktop/mouse/screen-size")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---- keyboard ----


async def test_keyboard_type_endpoint(client):
    with _patch(get_keyboard_engine, "type_text", _ok({"characters": 5})) as ty:
        resp = await client.post("/api/v1/desktop/keyboard/type", json={"text": "hello"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    ty.assert_called_once_with("hello", interval=0.0)


async def test_keyboard_press_endpoint(client):
    with _patch(get_keyboard_engine, "press", _ok({"key": "enter"})) as pr:
        resp = await client.post("/api/v1/desktop/keyboard/press", json={"key": "enter"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    pr.assert_called_once_with("enter")


async def test_keyboard_hotkey_endpoint(client):
    with _patch(get_keyboard_engine, "hotkey", _ok({"keys": ["ctrl", "c"]})) as hk:
        resp = await client.post("/api/v1/desktop/keyboard/hotkey", json={"keys": ["ctrl", "c"]})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    hk.assert_called_once_with(["ctrl", "c"])


# ---- clipboard ----


async def test_clipboard_read_endpoint(client):
    with _patch(get_clipboard_engine, "read", _ok({"content": "hi", "length": 2})):
        resp = await client.get("/api/v1/desktop/clipboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["content"] == "hi"


async def test_clipboard_write_endpoint(client):
    with _patch(get_clipboard_engine, "write", _ok({"length": 3})) as wr:
        resp = await client.post("/api/v1/desktop/clipboard", json={"text": "abc"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    wr.assert_called_once_with("abc")


# ---- applications ----


async def test_list_processes_endpoint(client):
    with _patch(get_app_inspect_engine, "list_processes", _ok({"count": 3})):
        resp = await client.get("/api/v1/desktop/processes?limit=10")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_find_process_endpoint(client):
    with _patch(get_app_inspect_engine, "find_process", _ok({"count": 1})) as fp:
        resp = await client.post("/api/v1/desktop/processes/find", json={"name": "python"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    fp.assert_called_once_with("python")


# ---- windows ----


async def test_list_windows_endpoint(client):
    payload = _ok({"count": 2, "windows": [{"hwnd": 1, "title": "A"}]})
    with _patch(get_windows_control_engine, "list_windows", payload):
        resp = await client.get("/api/v1/desktop/windows?limit=5")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["count"] == 2


async def test_windows_focus_endpoint(client):
    with _patch(get_windows_control_engine, "focus_window", _ok({"hwnd": 42})) as fw:
        resp = await client.post(
            "/api/v1/desktop/windows/focus", json={"hwnd": 42}
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    fw.assert_called_once_with(hwnd=42, title="")


async def test_windows_close_endpoint(client):
    with _patch(get_windows_control_engine, "close_window", _ok({"hwnd": 7})) as cw:
        resp = await client.post(
            "/api/v1/desktop/windows/close", json={"title": "calc"}
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    cw.assert_called_once_with(hwnd=None, title="calc")


async def test_windows_stream_emits_snapshots(client):
    fake = type(
        "R",
        (),
        {"error": None, "count": 1, "windows": [{"hwnd": 1, "title": "T"}]},
    )()
    with _patch(get_windows_control_engine, "list_windows", fake):
        async with client.stream("GET", "/api/v1/desktop/windows/stream") as resp:
            assert resp.status_code == 200
            chunks = ""
            async for chunk in resp.aiter_text():
                chunks += chunk
                break  # disconnect after the first snapshot
    assert "event: snapshot" in chunks
    assert '"count": 1' in chunks


# ---- monitors ----


async def test_list_monitors_endpoint(client):
    payload = _ok({"count": 1, "monitors": [{"index": 0, "is_primary": True}]})
    with _patch(get_windows_control_engine, "list_monitors", payload):
        resp = await client.get("/api/v1/desktop/monitors")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---- hotkeys ----


async def test_list_hotkeys_endpoint(client):
    payload = _ok({"count": 1, "hotkeys": [{"name": "a", "combo": ["ctrl", "k"]}]})
    with _patch(get_hotkey_engine, "list", payload):
        resp = await client.get("/api/v1/desktop/hotkeys")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_register_hotkey_endpoint(client):
    with _patch(get_hotkey_engine, "register", _ok({"count": 1})) as rg:
        resp = await client.post(
            "/api/v1/desktop/hotkeys",
            json={"name": "a", "combo": ["ctrl", "alt", "k"]},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    rg.assert_called_once_with("a", ["ctrl", "alt", "k"], enabled=True)


async def test_unregister_hotkey_endpoint(client):
    with _patch(get_hotkey_engine, "unregister", _ok({"count": 0})) as ur:
        resp = await client.delete("/api/v1/desktop/hotkeys/a")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    ur.assert_called_once_with("a")


# ---- unavailable surfaces as ok=False, not a crash ----


async def test_mouse_unavailable_returns_error_payload(client):
    err = _unavailable("Mouse backend not available on this platform.")
    with _patch(get_mouse_engine, "move", err):
        resp = await client.post("/api/v1/desktop/mouse/move", json={"x": 1, "y": 2})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "not available" in resp.json()["error"]


async def test_windows_unavailable_returns_error_payload(client):
    err = _unavailable("Window backend not available on this platform.")
    with _patch(get_windows_control_engine, "list_windows", err):
        resp = await client.get("/api/v1/desktop/windows")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "not available" in resp.json()["error"]
