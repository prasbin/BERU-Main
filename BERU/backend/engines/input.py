"""Mouse and keyboard input engines — real desktop interaction via pyautogui.

These engines perform genuine host input. They are intentionally separate from
the tool layer so that the safety gating (confirmation) lives at the tool/API
boundary while the engines stay minimal and testable.

If pyautogui is not installed (or the host platform is unsupported), the
engines report an ``unavailable`` state instead of raising or faking success.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from backend.engines.desktop_platform import HostPlatform

logger = logging.getLogger(__name__)


@dataclass
class InputResult:
    """Result of a mouse or keyboard action."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    action: str = ""
    performed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            "performed_at": self.performed_at.isoformat(),
            "detail": self.detail,
        }
        if self.error:
            result["error"] = self.error
        return result


def _load_pyautogui():
    """Return the pyautogui module or None if it cannot be imported."""
    try:
        import pyautogui

        return pyautogui
    except ImportError:
        return None


class MouseEngine:
    """Performs real mouse actions on the host desktop."""

    def __init__(self) -> None:
        self._platform = HostPlatform()
        self._pg = _load_pyautogui()
        self._available = bool(self._pg) and self._platform.is_supported
        if self._pg is not None:
            # Avoid tripping the pyautogui fail-safe while scripting input.
            self._pg.FAILSAFE = False

    def _validate_args(self, x: Any, y: Any) -> tuple[int, int] | None:
        try:
            xi = int(x)
            yi = int(y)
        except (TypeError, ValueError):
            return None
        if xi < 0 or yi < 0:
            return None
        return (xi, yi)

    def position(self) -> InputResult:
        """Return the current mouse cursor position."""
        result = InputResult(action="position")
        if not self._available:
            result.error = "Mouse backend not available on this platform."
            return result
        try:
            x, y = self._pg.position()
            result.detail = {"x": int(x), "y": int(y)}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Mouse position failed")
        return result

    def move(self, x: int, y: int, duration: float = 0.0) -> InputResult:
        """Move the mouse cursor to absolute coordinates."""
        result = InputResult(action="move")
        coords = self._validate_args(x, y)
        if coords is None:
            result.error = "move requires non-negative integer x and y."
            return result
        if not self._available:
            result.error = "Mouse backend not available on this platform."
            return result
        try:
            self._pg.moveTo(coords[0], coords[1], duration=duration)
            result.detail = {"x": coords[0], "y": coords[1]}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Mouse move failed")
        return result

    def click(
        self, x: int | None = None, y: int | None = None, button: str = "left"
    ) -> InputResult:
        """Click the current position or a given coordinate."""
        result = InputResult(action="click")
        if x is not None and y is not None:
            coords = self._validate_args(x, y)
            if coords is None:
                result.error = "click requires non-negative integer x and y."
                return result
        else:
            coords = None
        if not self._available:
            result.error = "Mouse backend not available on this platform."
            return result
        try:
            button = (button or "left").lower()
            if coords:
                self._pg.click(coords[0], coords[1], button=button)
            else:
                self._pg.click(button=button)
            result.detail = {"button": button, "clicked_at": coords or "current"}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Mouse click failed")
        return result

    def double_click(
        self, x: int | None = None, y: int | None = None, button: str = "left"
    ) -> InputResult:
        """Double-click the current position or a given coordinate."""
        result = InputResult(action="double_click")
        if x is not None and y is not None:
            coords = self._validate_args(x, y)
            if coords is None:
                result.error = "double_click requires non-negative integer x and y."
                return result
        else:
            coords = None
        if not self._available:
            result.error = "Mouse backend not available on this platform."
            return result
        try:
            button = (button or "left").lower()
            if coords:
                self._pg.doubleClick(coords[0], coords[1], button=button)
            else:
                self._pg.doubleClick(button=button)
            result.detail = {"button": button, "clicked_at": coords or "current"}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Mouse double-click failed")
        return result

    def scroll(self, amount: int, x: int | None = None, y: int | None = None) -> InputResult:
        """Scroll the wheel by ``amount`` clicks (positive = up, negative = down)."""
        result = InputResult(action="scroll")
        try:
            amt = int(amount)
        except (TypeError, ValueError):
            result.error = "scroll requires an integer amount."
            return result
        if not self._available:
            result.error = "Mouse backend not available on this platform."
            return result
        try:
            if x is not None and y is not None:
                coords = self._validate_args(x, y)
                if coords is None:
                    result.error = "scroll requires non-negative integer x and y."
                    return result
                self._pg.scroll(amt, x=coords[0], y=coords[1])
            else:
                self._pg.scroll(amt)
            result.detail = {"amount": amt}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Mouse scroll failed")
        return result

    def screen_size(self) -> InputResult:
        """Return the screen resolution."""
        result = InputResult(action="screen_size")
        if not self._available:
            result.error = "Mouse backend not available on this platform."
            return result
        try:
            width, height = self._pg.size()
            result.detail = {"width": int(width), "height": int(height)}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Screen size lookup failed")
        return result

    @property
    def is_available(self) -> bool:
        return self._available


class KeyboardEngine:
    """Performs real keyboard actions on the host desktop."""

    def __init__(self) -> None:
        self._platform = HostPlatform()
        self._pg = _load_pyautogui()
        self._available = bool(self._pg) and self._platform.is_supported
        if self._pg is not None:
            self._pg.FAILSAFE = False
            self._pg.PAUSE = 0.0

    def type_text(self, text: str, interval: float = 0.0) -> InputResult:
        """Type ``text`` at the focused field."""
        result = InputResult(action="type_text")
        if not isinstance(text, str):
            result.error = "type_text requires a string 'text' argument."
            return result
        if not self._available:
            result.error = "Keyboard backend not available on this platform."
            return result
        try:
            self._pg.typewrite(text, interval=interval)
            result.detail = {"characters": len(text)}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Keyboard typing failed")
        return result

    def press(self, key: str) -> InputResult:
        """Press and release a single key."""
        result = InputResult(action="press")
        if not isinstance(key, str) or not key.strip():
            result.error = "press requires a non-empty key name."
            return result
        if not self._available:
            result.error = "Keyboard backend not available on this platform."
            return result
        try:
            self._pg.press(key)
            result.detail = {"key": key}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Keyboard press failed")
        return result

    def hotkey(self, keys: list[str]) -> InputResult:
        """Press a combination of keys (e.g. ``["ctrl", "c"]``)."""
        result = InputResult(action="hotkey")
        if not keys or not all(isinstance(k, str) for k in keys):
            result.error = "hotkey requires a non-empty list of key names."
            return result
        if not self._available:
            result.error = "Keyboard backend not available on this platform."
            return result
        try:
            self._pg.hotkey(*keys)
            result.detail = {"keys": list(keys)}
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Keyboard hotkey failed")
        return result

    @property
    def is_available(self) -> bool:
        return self._available


@lru_cache
def get_mouse_engine() -> MouseEngine:
    """Return the process-wide mouse engine (shared by API + agent tools)."""
    return MouseEngine()


@lru_cache
def get_keyboard_engine() -> KeyboardEngine:
    """Return the process-wide keyboard engine (shared by API + agent tools)."""
    return KeyboardEngine()
