"""Window and monitor control engine for Windows hosts.

Provides real top-level window management (enumerate, find by title, focus,
request close) and multi-monitor geometry over the Win32 API via ``ctypes``
with no extra dependencies. On non-Windows hosts the engine reports an honest
``unavailable`` state instead of faking results (see ``docs/decisions.md`` #14).

The invasive Win32 boundary is isolated in module-level helpers so the engine
logic is testable hermetically by patching those helpers; the real helpers call
into ``user32`` only when running on Windows.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from backend.engines.desktop_platform import HostPlatform

logger = logging.getLogger(__name__)

# Win32 constants used by the boundary helpers.
_WM_CLOSE = 0x0010
_SW_RESTORE = 9
_MONITORINFOF_PRIMARY = 0x00000001

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)


class _MONITORINFO(ctypes.Structure):
    """``MONITORINFO`` layout (not defined by ``ctypes.wintypes``)."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _user32() -> Any | None:
    """Return the ``user32`` library handle, or ``None`` off Windows."""
    if platform.system() != "Windows":
        return None
    try:
        import ctypes

        return ctypes.windll.user32
    except Exception as exc:  # pragma: no cover - depends on host
        logger.warning("user32 unavailable: %s", exc)
        return None


def _setup(user32: Any) -> None:
    """Declare arg/return types once so pointer mismatches are avoided."""
    user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.RECT),
    ]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM,
    ]
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL


def _window_info(user32: Any, hwnd: int) -> dict[str, Any] | None:
    """Snapshot a single top-level window (or ``None`` when title is empty)."""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value.strip()
    if not title:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wintypes.RECT()
    try:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        left = top = right = bottom = 0
    return {
        "hwnd": int(hwnd),
        "title": title,
        "pid": int(pid.value),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "x": int(left),
        "y": int(top),
        "width": int(max(right - left, 0)),
        "height": int(max(bottom - top, 0)),
    }


def _enum_windows(max_windows: int = 200) -> list[dict[str, Any]]:
    """Enumerate top-level windows with a title, newest first."""
    user32 = _user32()
    if not user32:
        return []
    _setup(user32)
    found: list[dict[str, Any]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if len(found) >= max_windows:
            return False
        entry = _window_info(user32, hwnd)
        if entry is not None:
            found.append(entry)
        return True

    user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found


def _focus_hwnd(hwnd: int) -> bool:
    """Restore and bring the window to the foreground."""
    user32 = _user32()
    if not user32:
        return False
    _setup(user32)
    user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    return True


def _close_hwnd(hwnd: int) -> bool:
    """Post a graceful ``WM_CLOSE`` to the window."""
    user32 = _user32()
    if not user32:
        return False
    _setup(user32)
    return bool(user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0))


def _enum_monitors() -> list[dict[str, Any]]:
    """Enumerate display monitors with bounds and primary flag."""
    user32 = _user32()
    if not user32:
        return []
    _setup(user32)
    monitors: list[dict[str, Any]] = []
    index = 0

    def callback(hmon: int, _hdc: int, rect_ptr, _lparam: int) -> bool:
        nonlocal index
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        rect = rect_ptr.contents if rect_ptr else None
        if rect is None:
            return True
        monitors.append(
            {
                "index": index,
                "is_primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                "x": int(rect.left),
                "y": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
            }
        )
        index += 1
        return True

    user32.EnumDisplayMonitors(0, 0, _MONITORENUMPROC(callback), 0)
    return monitors


# ---- Result containers ----


@dataclass
class WindowsResult:
    """Result of a window/monitor enquiry or manipulation."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    operation: str = ""
    windows: list[dict[str, Any]] = field(default_factory=list)
    monitors: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    hwnd: int | None = None
    performed: bool | None = None
    inspected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "operation": self.operation,
            "count": self.count,
            "inspected_at": self.inspected_at.isoformat(),
        }
        if self.windows:
            result["windows"] = self.windows
        if self.monitors:
            result["monitors"] = self.monitors
        if self.hwnd is not None:
            result["hwnd"] = self.hwnd
        if self.performed is not None:
            result["performed"] = self.performed
        if self.error:
            result["error"] = self.error
        return result


class WindowsControlEngine:
    """Real top-level window and monitor control over the Win32 API."""

    def __init__(self) -> None:
        self._platform = HostPlatform()
        self._available = self._probe()

    def _probe(self) -> bool:
        return self._platform.is_windows and _user32() is not None

    # ---- Enquiry ----

    def list_windows(self, limit: int = 50) -> WindowsResult:
        """List top-level windows (with a title), most recent first."""
        result = WindowsResult(operation="list_windows")
        if not self._available:
            result.error = "Window backend not available on this platform."
            return result
        windows = _enum_windows()
        if limit and limit > 0:
            windows = windows[: int(limit)]
        result.windows = windows
        result.count = len(windows)
        return result

    def list_monitors(self) -> WindowsResult:
        """List display monitors with bounds and primary flag."""
        result = WindowsResult(operation="list_monitors")
        if not self._available:
            result.error = "Monitor backend not available on this platform."
            return result
        monitors = _enum_monitors()
        result.monitors = monitors
        result.count = len(monitors)
        return result

    # ---- Action ----

    def _resolve_hwnd(self, hwnd: int | None, title: str) -> int | None:
        if hwnd:
            return int(hwnd)
        if not title:
            return None
        needle = title.lower()
        for win in _enum_windows():
            if needle in win["title"].lower():
                return int(win["hwnd"])
        return None

    def focus_window(self, hwnd: int | None = None, title: str = "") -> WindowsResult:
        """Bring a window to the foreground (by handle or title match)."""
        result = WindowsResult(operation="focus_window")
        if not self._available:
            result.error = "Window backend not available on this platform."
            return result
        target = self._resolve_hwnd(hwnd, title)
        if not target:
            result.error = "focus_window needs a valid 'hwnd' or a matching window title."
            return result
        done = _focus_hwnd(target)
        result.hwnd = target
        result.performed = done
        if not done:
            result.error = "Failed to focus the window."
        return result

    def close_window(self, hwnd: int | None = None, title: str = "") -> WindowsResult:
        """Request a graceful close of a window (by handle or title match)."""
        result = WindowsResult(operation="close_window")
        if not self._available:
            result.error = "Window backend not available on this platform."
            return result
        target = self._resolve_hwnd(hwnd, title)
        if not target:
            result.error = "close_window needs a valid 'hwnd' or a matching window title."
            return result
        done = _close_hwnd(target)
        result.hwnd = target
        result.performed = done
        if not done:
            result.error = "Failed to post a close request to the window."
        return result

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def platform(self) -> str:
        return self._platform.name


@lru_cache
def get_windows_control_engine() -> WindowsControlEngine:
    """Return the process-wide window control engine."""
    return WindowsControlEngine()