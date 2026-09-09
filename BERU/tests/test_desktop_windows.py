"""Tests for window control and durable global hotkeys.

The Windows engine drives the real Win32 API, so the invasive boundary
(``_enum_windows``, ``_focus_hwnd``, ...) is patched module-level and the engine
logic — target resolution, result shaping, availability gating, persistence —
is exercised hermetically. Hotkey tests force the engine unavailable so no
listener thread or OS registration is ever created.
"""

from __future__ import annotations

from unittest.mock import patch

from backend.engines.hotkeys import HotkeyEngine, _parse_combo
from backend.engines.windows import WindowsControlEngine
from backend.tools.desktop import (
    CloseWindowTool,
    FocusWindowTool,
    ListHotkeysTool,
    ListMonitorsTool,
    ListWindowsTool,
    RegisterHotkeyTool,
    UnregisterHotkeyTool,
)

WINDOWS = [
    {
        "hwnd": 100, "title": "Notepad", "pid": 11, "visible": True,
        "x": 0, "y": 0, "width": 800, "height": 600,
    },
    {
        "hwnd": 200, "title": "Calculator", "pid": 22, "visible": True,
        "x": 10, "y": 10, "width": 320, "height": 240,
    },
    {
        "hwnd": 300, "title": "Hidden Helper", "pid": 33, "visible": False,
        "x": 0, "y": 0, "width": 100, "height": 100,
    },
]


def _unavailable_engine() -> WindowsControlEngine:
    engine = WindowsControlEngine()
    engine._available = False
    return engine


def _available_engine() -> WindowsControlEngine:
    engine = WindowsControlEngine()
    engine._available = True
    return engine


# ---- Windows engine ----


async def test_list_windows_success():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        result = engine.list_windows(limit=2)
    assert result.error is None
    assert result.count == 2
    assert result.windows[0]["title"] == "Notepad"


async def test_list_windows_honours_limit():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        result = engine.list_windows(limit=1)
    assert result.count == 1


async def test_list_windows_unavailable():
    engine = _unavailable_engine()
    result = engine.list_windows()
    assert result.error is not None
    assert "not available" in result.error


async def test_focus_window_by_hwnd():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        with patch("backend.engines.windows._focus_hwnd", return_value=True) as focus:
            result = engine.focus_window(hwnd=200)
    assert result.error is None
    assert result.hwnd == 200
    assert result.performed is True
    focus.assert_called_once_with(200)


async def test_focus_window_by_title():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        with patch("backend.engines.windows._focus_hwnd", return_value=True) as focus:
            result = engine.focus_window(title="calc")
    assert result.error is None
    assert result.hwnd == 200
    focus.assert_called_once_with(200)


async def test_focus_window_no_target():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        result = engine.focus_window()
    assert result.error is not None
    assert "hwnd" in result.error


async def test_focus_window_unmatched_title():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        result = engine.focus_window(title="nope")
    assert result.error is not None


async def test_close_window_by_hwnd():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        with patch("backend.engines.windows._close_hwnd", return_value=True) as close:
            result = engine.close_window(hwnd=300)
    assert result.error is None
    assert result.performed is True
    close.assert_called_once_with(300)


async def test_close_window_by_title():
    engine = _available_engine()
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        with patch("backend.engines.windows._close_hwnd", return_value=True) as close:
            result = engine.close_window(title="notepad")
    assert result.error is None
    assert result.hwnd == 100
    close.assert_called_once_with(100)


async def test_close_window_unavailable():
    engine = _unavailable_engine()
    result = engine.close_window(hwnd=100)
    assert result.error is not None
    assert "not available" in result.error


async def test_list_monitors_success():
    engine = _available_engine()
    monitors = [
        {"index": 0, "is_primary": True, "x": 0, "y": 0, "width": 1920, "height": 1080},
        {"index": 1, "is_primary": False, "x": 1920, "y": 0, "width": 1280, "height": 1024},
    ]
    with patch("backend.engines.windows._enum_monitors", return_value=monitors):
        result = engine.list_monitors()
    assert result.error is None
    assert result.count == 2
    assert result.monitors[0]["is_primary"] is True


async def test_list_monitors_unavailable():
    engine = _unavailable_engine()
    result = engine.list_monitors()
    assert result.error is not None


# ---- Window tools ----


def _win_tool_engine(engine):
    return lambda: engine


async def test_list_windows_tool(monkeypatch):
    from backend.tools import desktop as desktop_mod

    engine = _available_engine()
    monkeypatch.setattr(desktop_mod, "get_windows_control_engine", lambda: engine)
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        result = await ListWindowsTool().run(limit=10)
    assert result.ok is True
    assert result.output["count"] == 3


async def test_focus_window_tool_confirmation_and_run(monkeypatch):
    from backend.tools import desktop as desktop_mod

    tool = FocusWindowTool()
    assert tool.requires_confirmation is True
    engine = _available_engine()
    monkeypatch.setattr(desktop_mod, "get_windows_control_engine", lambda: engine)
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        with patch("backend.engines.windows._focus_hwnd", return_value=True):
            result = await tool.run(hwnd=100)
    assert result.ok is True


async def test_close_window_tool_confirmation_and_run(monkeypatch):
    from backend.tools import desktop as desktop_mod

    tool = CloseWindowTool()
    assert tool.requires_confirmation is True
    engine = _available_engine()
    monkeypatch.setattr(desktop_mod, "get_windows_control_engine", lambda: engine)
    with patch("backend.engines.windows._enum_windows", return_value=WINDOWS):
        with patch("backend.engines.windows._close_hwnd", return_value=True):
            result = await tool.run(title="calc")
    assert result.ok is True


async def test_list_monitors_tool(monkeypatch):
    from backend.tools import desktop as desktop_mod

    engine = _available_engine()
    monkeypatch.setattr(desktop_mod, "get_windows_control_engine", lambda: engine)
    monitors = [{"index": 0, "is_primary": True, "x": 0, "y": 0, "width": 1920, "height": 1080}]
    with patch("backend.engines.windows._enum_monitors", return_value=monitors):
        result = await ListMonitorsTool().run()
    assert result.ok is True
    assert result.output["count"] == 1


async def test_windows_tools_unavailable(monkeypatch):
    from backend.tools import desktop as desktop_mod

    engine = _unavailable_engine()
    monkeypatch.setattr(desktop_mod, "get_windows_control_engine", lambda: engine)
    result = await ListMonitorsTool().run()
    assert result.ok is False
    assert "not available" in result.error


# ---- Hotkey combo parsing ----


def test_parse_combo_letter_with_modifiers():
    mods, vk = _parse_combo(["ctrl", "alt", "k"])
    assert vk == ord("K")
    assert mods & 0x0002 and mods & 0x0001


def test_parse_combo_function_key():
    mods, vk = _parse_combo(["ctrl", "shift", "f5"])
    assert vk == 0x74
    assert mods & 0x0002 and mods & 0x0004


def test_parse_combo_special_key():
    mods, vk = _parse_combo(["alt", "enter"])
    assert vk == 0x0D


def test_parse_combo_requires_trigger_key():
    assert isinstance(_parse_combo(["ctrl", "alt"]), str)


def test_parse_combo_unknown_key():
    assert isinstance(_parse_combo(["ctrl", "xkcd"]), str)


# ---- Hotkey engine (no threads, no OS) ----


def _quiet_engine(state_file=None) -> HotkeyEngine:
    engine = HotkeyEngine(state_file=state_file, autostart=False)
    engine._available = False
    return engine


def test_hotkey_register_list_unregister():
    engine = _quiet_engine()
    reg = engine.register("launch", ["ctrl", "alt", "k"])
    assert reg.error is None
    assert reg.count == 1
    listed = engine.list()
    assert listed.count == 1
    assert listed.hotkeys[0]["name"] == "launch"
    assert listed.hotkeys[0]["combo"] == ["ctrl", "alt", "k"]

    removed = engine.unregister("launch")
    assert removed.error is None
    assert engine.list().count == 0


def test_hotkey_register_requires_name():
    engine = _quiet_engine()
    result = engine.register("", ["ctrl", "k"])
    assert result.error is not None
    assert "name" in result.error


def test_hotkey_register_bad_combo():
    engine = _quiet_engine()
    result = engine.register("x", ["ctrl"])
    assert result.error is not None


def test_hotkey_fire_tracks_stats():
    engine = _quiet_engine()
    engine.register("ping", ["ctrl", "f8"])
    engine.fire("ping")
    engine.fire("ping")
    listed = engine.list()
    assert listed.hotkeys[0]["fire_count"] == 2
    assert listed.hotkeys[0]["last_fired_at"] is not None


def test_hotkey_unregister_unknown():
    engine = _quiet_engine()
    result = engine.unregister("ghost")
    assert result.error is not None
    assert "ghost" in result.error


def test_hotkey_persistence_round_trip(tmp_path):
    state_file = tmp_path / "hotkeys.json"
    engine = _quiet_engine(state_file=str(state_file))
    engine.register("alpha", ["ctrl", "alt", "a"])
    engine.register("beta", ["ctrl", "f9"])
    engine.fire("beta")

    reloaded = _quiet_engine(state_file=str(state_file))
    assert reloaded.list().count == 2
    names = {h["name"] for h in reloaded.list().hotkeys}
    assert names == {"alpha", "beta"}
    beta = next(h for h in reloaded.list().hotkeys if h["name"] == "beta")
    assert beta["fire_count"] == 1
    assert beta["last_fired_at"] is not None


def test_hotkey_apply_registration_sets_flag():
    engine = _quiet_engine()
    engine.register("ping", ["ctrl", "f1"])
    with patch("backend.engines.hotkeys._register_os_hotkey", return_value=True) as reg:
        engine._apply_registration("ping", 0x0002, 0x70)
    reg.assert_called_once()
    listed = engine.list()
    assert listed.hotkeys[0]["registered"] is True


def test_hotkey_apply_registration_failure_stays_unregistered():
    engine = _quiet_engine()
    engine.register("ping", ["ctrl", "f1"])
    with patch("backend.engines.hotkeys._register_os_hotkey", return_value=False):
        engine._apply_registration("ping", 0x0002, 0x70)
    assert engine.list().hotkeys[0]["registered"] is False


# ---- Hotkey tools ----


def _hotkey_tool_engine():
    engine = HotkeyEngine()
    engine._available = False
    return engine


async def test_list_hotkeys_tool(monkeypatch):
    from backend.tools import desktop as desktop_mod

    engine = _hotkey_tool_engine()
    monkeypatch.setattr(desktop_mod, "get_hotkey_engine", lambda: engine)
    engine.register("a", ["ctrl", "k"])
    result = await ListHotkeysTool().run()
    assert result.ok is True
    assert result.output["count"] == 1


async def test_register_hotkey_tool_confirmation(monkeypatch):
    from backend.tools import desktop as desktop_mod

    tool = RegisterHotkeyTool()
    assert tool.requires_confirmation is True
    engine = _hotkey_tool_engine()
    monkeypatch.setattr(desktop_mod, "get_hotkey_engine", lambda: engine)
    result = await tool.run(name="a", combo=["ctrl", "alt", "k"])
    assert result.ok is True
    assert engine.list().count == 1


async def test_unregister_hotkey_tool_confirmation(monkeypatch):
    from backend.tools import desktop as desktop_mod

    tool = UnregisterHotkeyTool()
    assert tool.requires_confirmation is True
    engine = _hotkey_tool_engine()
    engine.register("a", ["ctrl", "k"])
    monkeypatch.setattr(desktop_mod, "get_hotkey_engine", lambda: engine)
    result = await tool.run(name="a")
    assert result.ok is True
    assert engine.list().count == 0