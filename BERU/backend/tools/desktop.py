"""Desktop control tools for agents.

Tools that let BERU perform real, safe desktop interaction: capture screenshots,
control the mouse and keyboard, read/write the clipboard, and inspect running
applications.

All tools delegate to the process-wide desktop engines (screenshot, input,
clipboard, apps). Input-injection and clipboard-write tools declare
``requires_confirmation=True`` so the existing confirmation flow gates their
execution. Read-only inspection tools do not require confirmation.

When a capability is not available on the host (missing backend library or
unsupported platform), the tool reports ``availability = "unavailable"`` and
is filtered out of the LLM tool list.
"""

from __future__ import annotations

from typing import Any

from backend.engines.apps import get_app_inspect_engine
from backend.engines.clipboard import get_clipboard_engine
from backend.engines.hotkeys import get_hotkey_engine
from backend.engines.input import get_keyboard_engine, get_mouse_engine
from backend.engines.screenshot import get_screenshot_engine
from backend.engines.windows import get_windows_control_engine
from backend.tools.base import Tool, ToolResult


class ScreenshotTool(Tool):
    """Capture the real desktop screen and save it as a PNG."""

    name = "screenshot"
    description = (
        "Capture the real desktop screen, save it to a PNG file, and return its "
        "path plus basic screen-state metadata (size, dominant colour, brightness)."
    )
    permissions = ["screenshot"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_screenshot_engine().capture()
        if result.error:
            return ToolResult.failure(f"Screenshot failed: {result.error}")
        return ToolResult.success(result.to_dict())


class ScreenSizeTool(Tool):
    """Return the current screen resolution."""

    name = "screen_size"
    description = "Return the current screen resolution (width and height)."
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_mouse_engine().screen_size()
        if result.error:
            return ToolResult.failure(f"Screen size lookup failed: {result.error}")
        return ToolResult.success(result.to_dict())


# ---- Mouse control ----

class MousePositionTool(Tool):
    """Return the current mouse cursor position."""

    name = "mouse_position"
    description = "Return the current mouse cursor position (x, y coordinates)."
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_mouse_engine().position()
        if result.error:
            return ToolResult.failure(f"Mouse position failed: {result.error}")
        return ToolResult.success(result.to_dict())


class MouseMoveTool(Tool):
    """Move the mouse cursor to absolute coordinates."""

    name = "mouse_move"
    description = "Move the mouse cursor to absolute screen coordinates (x, y)."
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Absolute x coordinate."},
            "y": {"type": "integer", "description": "Absolute y coordinate."},
            "duration": {
                "type": "number",
                "description": "Move duration in seconds (optional).",
            },
        },
        "required": ["x", "y"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_mouse_engine().move(
            int(kwargs.get("x", 0)),
            int(kwargs.get("y", 0)),
            duration=float(kwargs.get("duration") or 0.0),
        )
        if result.error:
            return ToolResult.failure(f"Mouse move failed: {result.error}")
        return ToolResult.success(result.to_dict())


class MouseClickTool(Tool):
    """Click at the current position or a given coordinate."""

    name = "mouse_click"
    description = (
        "Click at the current mouse position, or at an optional (x, y) coordinate, "
        "with an optional button ('left'/'right'/etc.)."
    )
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Optional absolute x coordinate."},
            "y": {"type": "integer", "description": "Optional absolute y coordinate."},
            "button": {
                "type": "string",
                "description": "Mouse button: left, right, middle (default left).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        x = kwargs.get("x")
        y = kwargs.get("y")
        result = get_mouse_engine().click(
            x=int(x) if x is not None else None,
            y=int(y) if y is not None else None,
            button=kwargs.get("button") or "left",
        )
        if result.error:
            return ToolResult.failure(f"Mouse click failed: {result.error}")
        return ToolResult.success(result.to_dict())


class MouseDoubleClickTool(Tool):
    """Double-click at the current position or a given coordinate."""

    name = "mouse_double_click"
    description = (
        "Double-click at the current mouse position, or at an optional (x, y) "
        "coordinate, with an optional button."
    )
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Optional absolute x coordinate."},
            "y": {"type": "integer", "description": "Optional absolute y coordinate."},
            "button": {
                "type": "string",
                "description": "Mouse button: left, right, middle (default left).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        x = kwargs.get("x")
        y = kwargs.get("y")
        result = get_mouse_engine().double_click(
            x=int(x) if x is not None else None,
            y=int(y) if y is not None else None,
            button=kwargs.get("button") or "left",
        )
        if result.error:
            return ToolResult.failure(f"Mouse double-click failed: {result.error}")
        return ToolResult.success(result.to_dict())


class MouseScrollTool(Tool):
    """Scroll the mouse wheel by a number of clicks."""

    name = "mouse_scroll"
    description = (
        "Scroll the mouse wheel by an integer number of clicks (positive = up, "
        "negative = down), optionally at a coordinate."
    )
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "amount": {
                "type": "integer",
                "description": "Number of scroll clicks (positive = up, negative = down).",
            },
            "x": {"type": "integer", "description": "Optional absolute x coordinate."},
            "y": {"type": "integer", "description": "Optional absolute y coordinate."},
        },
        "required": ["amount"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        x = kwargs.get("x")
        y = kwargs.get("y")
        result = get_mouse_engine().scroll(
            int(kwargs.get("amount", 0)),
            x=int(x) if x is not None else None,
            y=int(y) if y is not None else None,
        )
        if result.error:
            return ToolResult.failure(f"Mouse scroll failed: {result.error}")
        return ToolResult.success(result.to_dict())


# ---- Keyboard control ----

class KeyboardTypeTool(Tool):
    """Type text at the focused field."""

    name = "keyboard_type"
    description = "Type text at the currently focused field."
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to type."},
        },
        "required": ["text"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_keyboard_engine().type_text(kwargs.get("text", ""))
        if result.error:
            return ToolResult.failure(f"Keyboard typing failed: {result.error}")
        return ToolResult.success(result.to_dict())


class KeyboardPressTool(Tool):
    """Press and release a single key."""

    name = "keyboard_press"
    description = "Press and release a single key (e.g. 'enter', 'tab', 'a', 'esc')."
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "The key name to press."},
        },
        "required": ["key"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_keyboard_engine().press(kwargs.get("key", ""))
        if result.error:
            return ToolResult.failure(f"Keyboard press failed: {result.error}")
        return ToolResult.success(result.to_dict())


class KeyboardHotkeyTool(Tool):
    """Press a combination of keys (hotkey)."""

    name = "keyboard_hotkey"
    description = (
        "Press a combination of keys together (e.g. ['ctrl', 'c'] to copy). "
        "Provide the keys as a JSON array."
    )
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of keys to press together.",
            },
        },
        "required": ["keys"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        keys = kwargs.get("keys") or []
        if not isinstance(keys, list):
            return ToolResult.failure("keyboard_hotkey requires a 'keys' list argument.")
        result = get_keyboard_engine().hotkey(keys)
        if result.error:
            return ToolResult.failure(f"Keyboard hotkey failed: {result.error}")
        return ToolResult.success(result.to_dict())


# ---- Clipboard ----

class ClipboardReadTool(Tool):
    """Read the current clipboard text."""

    name = "clipboard_read"
    description = "Read the current text off the OS clipboard."
    permissions = ["clipboard_read"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_clipboard_engine().read()
        if result.error:
            return ToolResult.failure(f"Clipboard read failed: {result.error}")
        return ToolResult.success(result.to_dict())


class ClipboardWriteTool(Tool):
    """Write text to the OS clipboard."""

    name = "clipboard_write"
    description = "Replace the OS clipboard contents with the given text."
    permissions = ["clipboard_write"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to put on the clipboard."},
        },
        "required": ["text"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_clipboard_engine().write(kwargs.get("text", ""))
        if result.error:
            return ToolResult.failure(f"Clipboard write failed: {result.error}")
        return ToolResult.success(result.to_dict())


# ---- Application inspection ----

class ListProcessesTool(Tool):
    """List the running processes/applications."""

    name = "list_processes"
    description = (
        "List running processes and applications (pid, name, status), most "
        "CPU-active first. Read-only."
    )
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of processes to return (default 50).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_app_inspect_engine().list_processes(limit=kwargs.get("limit") or 50)
        if result.error:
            return ToolResult.failure(f"Process listing failed: {result.error}")
        return ToolResult.success(result.to_dict())


class FindProcessTool(Tool):
    """Find running processes by name."""

    name = "find_process"
    description = "Find running processes whose name contains the given text (read-only)."
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Substring to match process names."},
        },
        "required": ["name"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_app_inspect_engine().find_process(kwargs.get("name", ""))
        if result.error:
            return ToolResult.failure(f"Process lookup failed: {result.error}")
        return ToolResult.success(result.to_dict())


# ---- Window control ----

class ListWindowsTool(Tool):
    """List the open top-level windows (title, pid, geometry)."""

    name = "list_windows"
    description = (
        "List the open top-level windows (title, pid, bounds), most recent "
        "first. Read-only."
    )
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of windows to return (default 50).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_windows_control_engine().list_windows(limit=kwargs.get("limit") or 50)
        if result.error:
            return ToolResult.failure(f"Window listing failed: {result.error}")
        return ToolResult.success(result.to_dict())


class FocusWindowTool(Tool):
    """Bring a window to the foreground."""

    name = "focus_window"
    description = (
        "Bring a window to the foreground. Pass its numeric 'hwnd' (from "
        "'list_windows') or a 'title' substring to match."
    )
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "hwnd": {"type": "integer", "description": "Window handle to focus."},
            "title": {"type": "string", "description": "Window title substring to match."},
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        engine = get_windows_control_engine()
        result = engine.focus_window(
            hwnd=int(kwargs["hwnd"]) if kwargs.get("hwnd") is not None else None,
            title=kwargs.get("title") or "",
        )
        if result.error:
            return ToolResult.failure(f"Window focus failed: {result.error}")
        return ToolResult.success(result.to_dict())


class CloseWindowTool(Tool):
    """Request a graceful close of a window."""

    name = "close_window"
    description = (
        "Request a graceful close of a window. Pass its numeric 'hwnd' (from "
        "'list_windows') or a 'title' substring to match."
    )
    permissions = ["input"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "hwnd": {"type": "integer", "description": "Window handle to close."},
            "title": {"type": "string", "description": "Window title substring to match."},
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        engine = get_windows_control_engine()
        result = engine.close_window(
            hwnd=int(kwargs["hwnd"]) if kwargs.get("hwnd") is not None else None,
            title=kwargs.get("title") or "",
        )
        if result.error:
            return ToolResult.failure(f"Window close failed: {result.error}")
        return ToolResult.success(result.to_dict())


# ---- Monitors ----

class ListMonitorsTool(Tool):
    """List display monitors with their geometry."""

    name = "list_monitors"
    description = (
        "List display monitors with their screen bounds and primary flag. "
        "Read-only."
    )
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_windows_control_engine().list_monitors()
        if result.error:
            return ToolResult.failure(f"Monitor listing failed: {result.error}")
        return ToolResult.success(result.to_dict())


# ---- Global hotkeys (durable) ----

class ListHotkeysTool(Tool):
    """List durable global hotkeys and their fire statistics."""

    name = "list_hotkeys"
    description = "List registered global hotkeys and their fire statistics."
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_hotkey_engine().list()
        if result.error:
            return ToolResult.failure(f"Hotkey listing failed: {result.error}")
        return ToolResult.success(result.to_dict())


class RegisterHotkeyTool(Tool):
    """Register a durable OS-wide hotkey."""

    name = "register_hotkey"
    description = (
        "Register an OS-wide hotkey that persists across restarts. Provide "
        "'name' and a 'combo' like ['ctrl', 'alt', 'k'] or ['ctrl', 'shift', "
        "'f5']. Fires are tracked and reported by 'list_hotkeys'."
    )
    permissions = ["write"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Stable name for the hotkey."},
            "combo": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keys, e.g. ['ctrl', 'alt', 'k'].",
            },
            "enabled": {
                "type": "boolean",
                "description": "Whether the hotkey is active (default true).",
            },
        },
        "required": ["name", "combo"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_hotkey_engine().register(
            kwargs.get("name", ""),
            list(kwargs.get("combo") or []),
            enabled=bool(kwargs.get("enabled", True)),
        )
        if result.error:
            return ToolResult.failure(f"Hotkey registration failed: {result.error}")
        return ToolResult.success(result.to_dict())


class UnregisterHotkeyTool(Tool):
    """Remove a durable global hotkey."""

    name = "unregister_hotkey"
    description = "Remove a previously registered global hotkey by name."
    permissions = ["write"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the hotkey to remove."},
        },
        "required": ["name"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        result = get_hotkey_engine().unregister(kwargs.get("name", ""))
        if result.error:
            return ToolResult.failure(f"Hotkey removal failed: {result.error}")
        return ToolResult.success(result.to_dict())
