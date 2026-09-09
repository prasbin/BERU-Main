"""Desktop control API endpoints: screenshot, mouse, keyboard, clipboard, apps.

These endpoints drive the same process-wide desktop engines used by the agent
tools (see :mod:`backend.tools.desktop`), so a UI panel and an agent share one
execution surface. Direct calls here are gated by API authentication
(``require_api_key``) and are user-initiated from the panel; the LLM path
additionally gates invasive tools via ``requires_confirmation`` in the tool
layer.

When a capability is unavailable on the host, endpoints return an ``error`` in
the payload (and the availability endpoint reports it) rather than faking a
result.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.api.security import require_api_key
from backend.engines.apps import get_app_inspect_engine
from backend.engines.clipboard import get_clipboard_engine
from backend.engines.hotkeys import get_hotkey_engine
from backend.engines.input import get_keyboard_engine, get_mouse_engine
from backend.engines.screenshot import get_screenshot_engine
from backend.engines.windows import get_windows_control_engine

router = APIRouter(
    prefix="/desktop",
    tags=["desktop"],
    dependencies=[Depends(require_api_key)],
)

_screenshot = get_screenshot_engine()
_clipboard = get_clipboard_engine()
_mouse = get_mouse_engine()
_keyboard = get_keyboard_engine()
_apps = get_app_inspect_engine()
_windows = get_windows_control_engine()
_hotkeys = get_hotkey_engine()


# ---- Request models ----


class MoveRequest(BaseModel):
    x: int
    y: int
    duration: float = 0.0


class PointRequest(BaseModel):
    x: int | None = None
    y: int | None = None
    button: str = "left"


class ScrollRequest(BaseModel):
    amount: int
    x: int | None = None
    y: int | None = None


class TypeRequest(BaseModel):
    text: str
    interval: float = 0.0


class PressRequest(BaseModel):
    key: str


class HotkeyRequest(BaseModel):
    keys: list[str]


class ClipboardWriteRequest(BaseModel):
    text: str


class FindProcessRequest(BaseModel):
    name: str


class WindowTargetRequest(BaseModel):
    hwnd: int | None = None
    title: str = ""


class HotkeyRegisterRequest(BaseModel):
    name: str
    combo: list[str]
    enabled: bool = True


# ---- Availability ----


@router.get("/status", summary="Desktop capability availability")
async def desktop_status() -> dict:
    return {
        "screenshot": _screenshot.is_available,
        "mouse": _mouse.is_available,
        "keyboard": _keyboard.is_available,
        "clipboard": _clipboard.is_available,
        "apps": _apps.is_available,
        "windows": _windows.is_available,
        "monitors": _windows.is_available,
        "hotkeys": _hotkeys.is_available,
        "screen_size": _mouse.is_available,
    }


# ---- Screenshot ----


@router.post("/screenshot", summary="Capture the desktop screen as a PNG")
async def screenshot() -> dict:
    result = _screenshot.capture()
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


# ---- Mouse ----


@router.get("/mouse/position", summary="Current mouse cursor position")
async def mouse_position() -> dict:
    result = _mouse.position()
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.get("/mouse/screen-size", summary="Current screen resolution")
async def mouse_screen_size() -> dict:
    result = _mouse.screen_size()
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/mouse/move", summary="Move the mouse cursor")
async def mouse_move(body: MoveRequest) -> dict:
    result = _mouse.move(body.x, body.y, duration=body.duration)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/mouse/click", summary="Click at a position or the cursor")
async def mouse_click(body: PointRequest) -> dict:
    result = _mouse.click(x=body.x, y=body.y, button=body.button)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/mouse/double-click", summary="Double-click at a position or the cursor")
async def mouse_double_click(body: PointRequest) -> dict:
    result = _mouse.double_click(x=body.x, y=body.y, button=body.button)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/mouse/scroll", summary="Scroll the mouse wheel")
async def mouse_scroll(body: ScrollRequest) -> dict:
    result = _mouse.scroll(body.amount, x=body.x, y=body.y)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


# ---- Keyboard ----


@router.post("/keyboard/type", summary="Type text at the focused field")
async def keyboard_type(body: TypeRequest) -> dict:
    result = _keyboard.type_text(body.text, interval=body.interval)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/keyboard/press", summary="Press and release a single key")
async def keyboard_press(body: PressRequest) -> dict:
    result = _keyboard.press(body.key)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/keyboard/hotkey", summary="Press a combination of keys")
async def keyboard_hotkey(body: HotkeyRequest) -> dict:
    result = _keyboard.hotkey(body.keys)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


# ---- Clipboard ----


@router.get("/clipboard", summary="Read the current clipboard text")
async def clipboard_read() -> dict:
    result = _clipboard.read()
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/clipboard", summary="Write text to the clipboard")
async def clipboard_write(body: ClipboardWriteRequest) -> dict:
    result = _clipboard.write(body.text)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


# ---- Applications/processes ----


@router.get("/processes", summary="List running processes")
async def list_processes(limit: int = 50) -> dict:
    result = _apps.list_processes(limit=limit)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/processes/find", summary="Find running processes by name")
async def find_process(body: FindProcessRequest) -> dict:
    result = _apps.find_process(body.name)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


# ---- Windows ----

_WINDOW_STREAM_MAX = 60
_WINDOW_STREAM_INTERVAL = 1.0


@router.get("/windows", summary="List open top-level windows")
async def list_windows(limit: int = 50) -> dict:
    result = _windows.list_windows(limit=limit)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/windows/focus", summary="Bring a window to the foreground")
async def focus_window(body: WindowTargetRequest) -> dict:
    result = _windows.focus_window(hwnd=body.hwnd, title=body.title)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/windows/close", summary="Request a graceful window close")
async def close_window(body: WindowTargetRequest) -> dict:
    result = _windows.close_window(hwnd=body.hwnd, title=body.title)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.get(
    "/windows/stream",
    summary="Stream live window snapshots (Server-Sent Events)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def windows_stream() -> StreamingResponse:
    """Emit ``event: snapshot`` blocks with the current window list.

    Snapshots are produced approximately every second until the client
    disconnects (or a safety cap of 60 is reached).
    """

    async def event_source() -> AsyncIterator[str]:
        sent = 0
        try:
            while sent < _WINDOW_STREAM_MAX:
                result = _windows.list_windows(limit=50)
                payload = {
                    "sent": sent,
                    "ok": result.error is None,
                    "count": result.count,
                    "windows": result.windows,
                }
                if result.error:
                    payload["error"] = result.error
                yield f"event: snapshot\ndata: {json.dumps(payload)}\n\n"
                sent += 1
                await asyncio.sleep(_WINDOW_STREAM_INTERVAL)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- Monitors ----


@router.get("/monitors", summary="List display monitors with geometry")
async def list_monitors() -> dict:
    result = _windows.list_monitors()
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


# ---- Global hotkeys (durable) ----


@router.get("/hotkeys", summary="List durable global hotkeys")
async def list_hotkeys() -> dict:
    result = _hotkeys.list()
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.post("/hotkeys", summary="Register a durable global hotkey")
async def register_hotkey(body: HotkeyRegisterRequest) -> dict:
    result = _hotkeys.register(body.name, body.combo, enabled=body.enabled)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}


@router.delete("/hotkeys/{name}", summary="Remove a durable global hotkey")
async def unregister_hotkey(name: str) -> dict:
    result = _hotkeys.unregister(name)
    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, **result.to_dict()}
