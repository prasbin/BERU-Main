"""Tests for real desktop control: screenshot, mouse, keyboard, clipboard, apps.

Engines perform genuine host actions. To keep the test suite from moving the
user's real cursor or typing into their keyboard, the invasive input engines
are tested by mocking the underlying third-party boundary (the engine's
``_pg`` / pyautogui handle) while still exercising the engine's parameter
validation, result shaping, and availability gating.

Read-only engines (screenshot metadata, clipboard read, process inspection) are
tested against their real implementations where deterministic, with the
external library mocked where the environment cannot be relied upon.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend.agents.registry import get_agent_registry
from backend.engines.apps import AppInspectEngine
from backend.engines.clipboard import ClipboardEngine
from backend.engines.input import KeyboardEngine, MouseEngine
from backend.engines.screenshot import ScreenshotEngine
from backend.tools.desktop import (
    ClipboardReadTool,
    ClipboardWriteTool,
    FindProcessTool,
    KeyboardHotkeyTool,
    KeyboardPressTool,
    KeyboardTypeTool,
    ListProcessesTool,
    MouseClickTool,
    MouseDoubleClickTool,
    MouseMoveTool,
    MousePositionTool,
    MouseScrollTool,
    ScreenshotTool,
)

# ---- Screenshot ----


def test_screenshot_engine_probe_on_supported_host():
    engine = ScreenshotEngine()
    # On a supported, library-enabled host the engine must not lie about
    # availability.
    if engine._platform.is_supported:
        assert engine._probe() is True or engine._available is True
    assert engine._platform.is_supported is True


async def test_screenshot_tool_requires_confirmation_and_validates():
    tool = ScreenshotTool()
    assert tool.permissions == ["screenshot"]
    # Screenshots can capture sensitive on-screen content; require confirmation.
    assert tool.requires_confirmation is True


@patch.object(ScreenshotEngine, "capture")
async def test_screenshot_tool_success(capture):
    capture.return_value = type(
        "R",
        (),
        {"error": None, "to_dict": lambda s: {"path": "/tmp/x.png", "width": 8}},
    )()
    result = await ScreenshotTool().run()
    assert result.ok is True
    assert result.output["path"] == "/tmp/x.png"


@patch.object(ScreenshotEngine, "capture")
async def test_screenshot_tool_failure_when_engine_reports_error(capture):
    capture.return_value = type(
        "R", (), {"error": "no backend", "to_dict": lambda s: {}}
    )()
    result = await ScreenshotTool().run()
    assert result.ok is False
    assert "Screenshot failed" in result.error


# ---- Mouse engine (mock pyautogui boundary) ----


def _fake_pg():
    pg = MagicMock()
    pg.position.return_value = (10, 20)
    pg.size.return_value = (1920, 1080)
    return pg


def test_mouse_move_validates_coordinates():
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    bad = engine.move(-1, 5)  # negative not allowed
    assert bad.error is not None
    good = engine.move(100, 200)
    assert good.error is None
    assert good.detail == {"x": 100, "y": 200}
    engine._pg.moveTo.assert_called_with(100, 200, duration=0.0)


def test_mouse_click_calls_pyautogui():
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    result = engine.click(x=5, y=6, button="right")
    assert result.error is None
    engine._pg.click.assert_called_with(5, 6, button="right")


def test_mouse_double_click_calls_pyautogui():
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    result = engine.double_click()
    assert result.error is None
    engine._pg.doubleClick.assert_called_with(button="left")


def test_mouse_scroll_calls_pyautogui():
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    result = engine.scroll(-3)
    assert result.error is None
    engine._pg.scroll.assert_called_with(-3)


def test_mouse_scroll_requires_int():
    engine = MouseEngine()
    result = engine.scroll("not-an-int")
    assert result.error is not None
    assert "integer" in result.error


def test_mouse_unavailable_reports_error():
    engine = MouseEngine()
    engine._available = False
    result = engine.move(10, 10)
    assert result.error is not None
    assert "not available" in result.error


# ---- Mouse tools ----


async def test_mouse_position_tool():
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(MouseEngine, "position", return_value=engine.position()):
        result = await MousePositionTool().run()
    assert result.ok is True
    assert result.output["detail"] == {"x": 10, "y": 20}


async def test_mouse_move_tool_gates_confirmation_and_runs():
    tool = MouseMoveTool()
    assert tool.requires_confirmation is True
    assert tool.permissions == ["input"]
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(MouseEngine, "move", return_value=engine.move(1, 2)) as mv:
        result = await tool.run(x=1, y=2)
    assert result.ok is True
    mv.assert_called_once_with(1, 2, duration=0.0)


async def test_mouse_click_tool_runs():
    tool = MouseClickTool()
    assert tool.requires_confirmation is True
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(MouseEngine, "click", return_value=engine.click(x=1, y=2)) as cl:
        result = await tool.run(x=1, y=2)
    assert result.ok is True
    cl.assert_called_once_with(x=1, y=2, button="left")


async def test_mouse_double_click_tool_runs():
    tool = MouseDoubleClickTool()
    assert tool.requires_confirmation is True
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(MouseEngine, "double_click", return_value=engine.double_click()):
        result = await tool.run()
    assert result.ok is True


async def test_mouse_scroll_tool_runs():
    tool = MouseScrollTool()
    assert tool.requires_confirmation is True
    engine = MouseEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(MouseEngine, "scroll", return_value=engine.scroll(4)) as sc:
        result = await tool.run(amount=4)
    assert result.ok is True
    sc.assert_called_once_with(4, x=None, y=None)


# ---- Keyboard engine ----


def test_keyboard_type_from_widget():
    engine = KeyboardEngine()
    engine._pg = _fake_pg()
    engine._available = True
    result = engine.type_text("hello")
    assert result.error is None
    engine._pg.typewrite.assert_called_with("hello", interval=0.0)


def test_keyboard_type_requires_string():
    engine = KeyboardEngine()
    engine._available = True
    result = engine.type_text(123)
    assert result.error is not None


def test_keyboard_press_from_widget():
    engine = KeyboardEngine()
    engine._pg = _fake_pg()
    engine._available = True
    result = engine.press("enter")
    assert result.error is None
    engine._pg.press.assert_called_with("enter")


def test_keyboard_hotkey_from_widget():
    engine = KeyboardEngine()
    engine._pg = _fake_pg()
    engine._available = True
    result = engine.hotkey(["ctrl", "c"])
    assert result.error is None
    engine._pg.hotkey.assert_called_with("ctrl", "c")


# ---- Keyboard tools ----


async def test_keyboard_type_tool_gates_and_runs():
    tool = KeyboardTypeTool()
    assert tool.requires_confirmation is True
    engine = KeyboardEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(KeyboardEngine, "type_text", return_value=engine.type_text("hi")):
        result = await tool.run(text="hi")
    assert result.ok is True


async def test_keyboard_press_tool_gates_and_runs():
    tool = KeyboardPressTool()
    assert tool.requires_confirmation is True
    engine = KeyboardEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(KeyboardEngine, "press", return_value=engine.press("esc")):
        result = await tool.run(key="esc")
    assert result.ok is True


async def test_keyboard_hotkey_tool_gates_and_runs():
    tool = KeyboardHotkeyTool()
    assert tool.requires_confirmation is True
    engine = KeyboardEngine()
    engine._pg = _fake_pg()
    engine._available = True
    with patch.object(KeyboardEngine, "hotkey", return_value=engine.hotkey(["win", "r"])):
        result = await tool.run(keys=["win", "r"])
    assert result.ok is True


# ---- Clipboard ----


def test_clipboard_read_from_widget():
    engine = ClipboardEngine()
    if not engine.is_available:
        # host without a clipboard backend must fail honestly, not fake
        r = engine.read()
        assert r.error is not None
        return
    with patch("pyperclip.paste", return_value="hello world"):
        r = engine.read()
    assert r.error is None
    assert r.content == "hello world"
    assert r.length == 11


def test_clipboard_write_from_widget():
    engine = ClipboardEngine()
    if not engine.is_available:
        return
    with patch("pyperclip.copy") as copy:
        r = engine.write("payload")
    assert r.error is None
    copy.assert_called_with("payload")
    assert r.length == 7


def test_clipboard_unavailable_is_honest():
    engine = ClipboardEngine()
    engine._available = False
    r = engine.read()
    assert r.error is not None
    assert "not available" in r.error


async def test_clipboard_read_tool_requires_confirmation():
    tool = ClipboardReadTool()
    # Clipboard contents can include passwords and secrets; require confirmation.
    assert tool.requires_confirmation is True
    with patch.object(ClipboardEngine, "read", return_value=type(
        "R", (), {"error": None, "to_dict": lambda s: {"content": "x", "length": 1}}
    )()):
        result = await tool.run()
    assert result.ok is True


async def test_clipboard_write_tool_gates_confirmation():
    tool = ClipboardWriteTool()
    assert tool.requires_confirmation is True
    assert tool.permissions == ["clipboard_write"]
    with patch.object(ClipboardEngine, "write", return_value=type(
        "R", (), {"error": None, "to_dict": lambda s: {"length": 2}}
    )()):
        result = await tool.run(text="ab")
    assert result.ok is True


# ---- Application inspection ----


def test_app_inspect_engine_probe():
    engine = AppInspectEngine()
    assert engine._platform.is_supported is True


def test_list_processes_real_or_mocked():
    engine = AppInspectEngine()
    if not engine.is_available:
        r = engine.list_processes()
        assert r.error is not None
        return
    with patch("psutil.process_iter", return_value=[]) as pi:
        r = engine.list_processes(limit=5)
    assert r.error is None
    assert r.count == 0
    assert r.operation == "list_processes"
    pi.assert_called_once()


def test_find_process_matches_name():
    engine = AppInspectEngine()
    if not engine.is_available:
        return
    proc = MagicMock()
    proc.info = {"pid": 1, "name": "python.exe", "status": "running"}
    with patch("psutil.process_iter", return_value=[proc]):
        r = engine.find_process("python")
    assert r.error is None
    assert r.count == 1
    assert r.processes[0]["name"] == "python.exe"


def test_find_process_requires_name():
    engine = AppInspectEngine()
    r = engine.find_process("")
    assert r.error is not None
    assert "non-empty" in r.error


async def test_list_processes_tool_no_confirmation():
    tool = ListProcessesTool()
    assert tool.requires_confirmation is False
    with patch.object(AppInspectEngine, "list_processes", return_value=type(
        "R", (), {"error": None, "to_dict": lambda s: {"count": 0}}
    )()):
        result = await tool.run()
    assert result.ok is True


async def test_find_process_tool_runs():
    tool = FindProcessTool()
    assert tool.requires_confirmation is False
    with patch.object(AppInspectEngine, "find_process", return_value=type(
        "R", (), {"error": None, "to_dict": lambda s: {"count": 1}}
    )()):
        result = await tool.run(name="python")
    assert result.ok is True


# ---- Agent registration / integration ----


def test_core_agent_exposes_desktop_tools_to_llm():
    registry = get_agent_registry()
    core = registry.get("beru_core")
    available = [t.name for t in core.tool_definitions]

    for name in [
        "screenshot",
        "screen_size",
        "mouse_position",
        "mouse_move",
        "mouse_click",
        "mouse_double_click",
        "mouse_scroll",
        "keyboard_type",
        "keyboard_press",
        "keyboard_hotkey",
        "clipboard_read",
        "clipboard_write",
        "list_processes",
        "find_process",
        "list_windows",
        "focus_window",
        "close_window",
        "list_monitors",
        "list_hotkeys",
        "register_hotkey",
        "unregister_hotkey",
    ]:
        assert name in available, f"desktop tool '{name}' not exposed to the LLM"
