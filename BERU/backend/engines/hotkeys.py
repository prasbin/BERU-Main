"""Durable global hotkey registry.

Lets BERU register OS-wide hotkeys that survive restarts: registrations (name,
key combo, enable state) and fire statistics are persisted to a JSON state file
and re-applied to the OS on startup. A background listener thread owns the
Win32 message queue (``RegisterHotKey`` + ``PeekMessage``) so dom and service
threads can register/unregister safely without fighting the OS message-pump
restrictions.

On non-Windows hosts the engine reports an honest ``unavailable`` state; the
logic itself (persistence, validation, bookkeeping) stays fully testable.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.engines.desktop_platform import HostPlatform
from backend.engines.windows import _setup, _user32

logger = logging.getLogger(__name__)

# Win32 constants.
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_PM_REMOVE = 0x0001
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008

_MODIFIERS = {
    "ctrl": _MOD_CONTROL,
    "control": _MOD_CONTROL,
    "alt": _MOD_ALT,
    "shift": _MOD_SHIFT,
    "win": _MOD_WIN,
    "meta": _MOD_WIN,
}

_SPECIAL_VK = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "capslock": 0x14, "printscreen": 0x2C,
}

_DEFAULT_STATE_FILE = Path("data") / "hotkeys.json"


def _register_os_hotkey(ident: int, mods: int, vk: int) -> bool:
    """Register a system-wide hotkey on the calling thread."""
    user32 = _user32()
    if not user32:
        return False
    _setup(user32)
    result = user32.RegisterHotKey(0, ident, mods, vk)
    return bool(result)


def _unregister_os_hotkey(ident: int) -> bool:
    """Unregister a system-wide hotkey bound to this thread."""
    user32 = _user32()
    if not user32:
        return False
    _setup(user32)
    result = user32.UnregisterHotKey(0, ident)
    return bool(result)


# ---- Result containers ----


@dataclass
class RegisteredHotkey:
    """A durable registered hotkey."""
    name: str = ""
    combo: list[str] = field(default_factory=list)
    enabled: bool = True
    registered: bool = False
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_fired_at: datetime | None = None
    fire_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "combo": self.combo,
            "enabled": self.enabled,
            "registered": self.registered,
            "registered_at": self.registered_at.isoformat(),
            "fire_count": self.fire_count,
        }
        if self.last_fired_at is not None:
            result["last_fired_at"] = self.last_fired_at.isoformat()
        return result


def _hotkey_from_dict(data: dict[str, Any]) -> RegisteredHotkey:
    try:
        last = data.get("last_fired_at")
        last_fired = datetime.fromisoformat(last) if last else None
    except (TypeError, ValueError):
        last_fired = None
    try:
        registered_at = datetime.fromisoformat(data["registered_at"])
    except (KeyError, TypeError, ValueError):
        registered_at = datetime.now(timezone.utc)
    return RegisteredHotkey(
        name=str(data.get("name", "")),
        combo=list(data.get("combo", [])),
        enabled=bool(data.get("enabled", True)),
        registered=bool(data.get("registered", False)),
        registered_at=registered_at,
        last_fired_at=last_fired,
        fire_count=int(data.get("fire_count", 0)),
    )


class HotkeyResult:
    """Result of a hotkey registration operation."""

    def __init__(
        self,
        operation: str,
        hotkeys: list[dict[str, Any]] | None = None,
        count: int = 0,
        message: str = "",
        error: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.operation = operation
        self.hotkeys = hotkeys or []
        self.count = count
        self.message = message
        self.error = error
        self.occurred_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "operation": self.operation,
            "count": self.count,
            "message": self.message,
            "occurred_at": self.occurred_at,
        }
        if self.hotkeys:
            result["hotkeys"] = self.hotkeys
        if self.error:
            result["error"] = self.error
        return result


def _parse_combo(combo: list[str] | tuple[str, ...]) -> tuple[int, int] | str:
    """Convert a combo like ['ctrl','alt','k'] to (modifiers, virtual-key)."""
    mods = 0
    vk: int | None = None
    for raw in combo:
        key = str(raw).strip().lower()
        if not key:
            continue
        if key in _MODIFIERS:
            mods |= _MODIFIERS[key]
            continue
        if key in _SPECIAL_VK:
            vk = _SPECIAL_VK[key]
            continue
        if len(key) >= 2 and key[0] == "f" and key[1:].isdigit():
            num = int(key[1:])
            if 1 <= num <= 24:
                vk = 0x70 + (num - 1)
                continue
        if len(key) == 1 and key.isalnum():
            vk = ord(key.upper())
            continue
        return f"Unknown key in combo: {raw!r}"
    if vk is None:
        return "Combo must include at least one non-modifier key."
    return mods, vk


class HotkeyEngine:
    """Registry of durable OS-wide hotkeys with a background listener."""

    def __init__(self, state_file: str | Path | None = None, autostart: bool = False) -> None:
        self._platform = HostPlatform()
        self._available = self._probe()
        self._state_file: Path | None = Path(state_file) if state_file else None
        self._hotkeys: dict[str, RegisteredHotkey] = {}
        self._by_ident: dict[int, str] = {}
        self._lock = threading.Lock()
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()
        if autostart:
            self.start()

    def _probe(self) -> bool:
        return self._platform.is_windows and _user32() is not None

    # ---- Persistence ----

    def _load(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            import json

            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            for entry in data.get("hotkeys", []):
                hk = _hotkey_from_dict(entry)
                if hk.name:
                    self._hotkeys[hk.name] = hk
            self._rebuild_ident_map()
        except Exception as exc:
            logger.warning("Could not load hotkey state from %s: %s", self._state_file, exc)

    def _persist(self) -> None:
        if not self._state_file:
            return
        try:
            import json

            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"hotkeys": [hk.to_dict() for hk in self._hotkeys.values()]}
            self._state_file.write_text(
                json.dumps(payload, indent=2), encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not persist hotkey state: %s", exc)

    # ---- Combo handling ----

    def _rebuild_ident_map(self) -> None:
        self._by_ident = {}
        for name, hk in self._hotkeys.items():
            ident = self._ident_for(name)
            self._by_ident[ident] = name
            hk.registered = False

    @staticmethod
    def _ident_for(name: str) -> int:
        # Stable, collision-resistant id in the RegisterHotKey wParam range.
        return (abs(hash(name)) % 32760) + 1

    # ---- Public API ----

    @property
    def is_available(self) -> bool:
        return self._available

    def list(self) -> HotkeyResult:
        """Return all registered hotkeys (durable state)."""
        with self._lock:
            items = [hk.to_dict() for hk in self._hotkeys.values()]
        return HotkeyResult(
            operation="list_hotkeys",
            hotkeys=items,
            count=len(items),
            message="OK",
        )

    def register(
        self, name: str, combo: list[str], enabled: bool = True,
    ) -> HotkeyResult:
        """Register (or update) a durable hotkey and bind it to the OS."""
        name = str(name or "").strip()
        if not name:
            return HotkeyResult(
                operation="register_hotkey", error="register_hotkey requires a 'name'."
            )
        parsed = _parse_combo(list(combo or []))
        if isinstance(parsed, str):
            return HotkeyResult(operation="register_hotkey", error=parsed)
        mods, vk = parsed

        with self._lock:
            existing = self._hotkeys.get(name)
            if existing:
                existing.combo = list(combo)
                existing.enabled = enabled
                existing.registered = False
            else:
                self._hotkeys[name] = RegisteredHotkey(
                    name=name, combo=list(combo), enabled=enabled,
                )
            self._persist()

        if self._running() and self._available:
            self._commands.put(("register", name, mods, vk))
        elif self._available:
            # No listener yet: start lazily, registration applies on start.
            self.start()
            self._commands.put(("register", name, mods, vk))
        return HotkeyResult(
            operation="register_hotkey",
            count=len(self._hotkeys),
            message=f"Hotkey '{name}' registered.",
        )

    def unregister(self, name: str) -> HotkeyResult:
        """Remove a durable hotkey and unbind it from the OS."""
        name = str(name or "").strip()
        with self._lock:
            hk = self._hotkeys.pop(name, None)
            if hk is None:
                return HotkeyResult(
                    operation="unregister_hotkey",
                    error=f"No hotkey named '{name}'.",
                )
            self._persist()
        if self._running() and self._available:
            assert self._thread is not None
            self._commands.put(("unregister", name))
        return HotkeyResult(
            operation="unregister_hotkey",
            count=len(self._hotkeys),
            message=f"Hotkey '{name}' removed.",
        )

    def fire(self, name: str) -> None:
        """Record that a hotkey fired (also called by the listener thread)."""
        with self._lock:
            hk = self._hotkeys.get(name)
            if not hk:
                return
            hk.fire_count += 1
            hk.last_fired_at = datetime.now(timezone.utc)
            self._persist()
        logger.info("Hotkey '%s' fired (total %s)", name, self._hotkeys[name].fire_count)

    # ---- Listener thread ----

    def _running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        """Start the message-pump listener thread (idempotent)."""
        if not self._available:
            return False
        if self._running():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="beru-hotkey-listener", daemon=True,
        )
        self._thread.start()
        # Re-apply persisted enabled hotkeys to the OS on the listener thread.
        with self._lock:
            for name, hk in self._hotkeys.items():
                self._by_ident[self._ident_for(name)] = name
                parsed = _parse_combo(hk.combo)
                if hk.enabled and not isinstance(parsed, str):
                    self._commands.put(("register", name, parsed[0], parsed[1]))
        return True

    def shutdown(self) -> None:
        """Stop the listener thread and release OS hotkey bindings."""
        if self._thread and self._thread.is_alive():
            self._stop.set()
            user32 = _user32()
            tid = self._thread.ident
            if user32 and tid:
                _setup(user32)
                user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
            self._thread.join(timeout=3.0)
        self._thread = None
        for ident in list(self._by_ident.keys()):
            if self._available:
                _unregister_os_hotkey(ident)
        self._by_ident.clear()

    def _loop(self) -> None:
        """Pump the message queue and apply registration commands."""
        user32 = _user32()
        if not user32:
            return
        _setup(user32)
        while not self._stop.is_set():
            self._drain_commands()
            self._pump_messages(user32)
            self._stop.wait(0.1)

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            op = cmd[0]
            if op == "register":
                self._apply_registration(cmd[1], cmd[2], cmd[3])
            elif op == "unregister":
                self._apply_unregistration(cmd[1])

    def _apply_registration(self, name: str, mods: int, vk: int) -> None:
        ident = self._ident_for(name)
        ok = _register_os_hotkey(ident, mods, vk)
        with self._lock:
            hk = self._hotkeys.get(name)
            if hk:
                hk.registered = ok
                self._by_ident[ident] = name
                self._persist()
        if ok:
            logger.info("Bound hotkey '%s' (%s) to the OS", name, hk.combo if hk else "")
        else:
            logger.warning("Failed to bind hotkey '%s'", name)

    def _apply_unregistration(self, name: str) -> None:
        ident = self._ident_for(name)
        _unregister_os_hotkey(ident)
        with self._lock:
            self._by_ident.pop(ident, None)

    def _pump_messages(self, user32: Any) -> None:
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, _PM_REMOVE):
            if msg.message == _WM_HOTKEY:
                name = self._by_ident.get(int(msg.wParam))
                if name:
                    self.fire(name)


@lru_cache
def get_hotkey_engine() -> HotkeyEngine:
    """Return the process-wide durable hotkey engine (autostart disabled).

    Persists to the default state file so registrations survive restarts.
    """
    return HotkeyEngine(state_file=_DEFAULT_STATE_FILE)