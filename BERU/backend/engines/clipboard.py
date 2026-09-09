"""Clipboard engine — read and write the real OS clipboard.

Uses ``pyperclip`` for the actual transfer. When the clipboard backend is not
installed (or the host lacks a supported platform), the engine reports an
``unavailable`` state rather than faking a result.
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
class ClipboardResult:
    """Result of a clipboard read or write."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    operation: str = ""
    content: str = ""
    length: int = 0
    changed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "operation": self.operation,
            "length": self.length,
            "changed_at": self.changed_at.isoformat(),
        }
        if self.operation == "read":
            result["content"] = self.content
        if self.error:
            result["error"] = self.error
        return result


class ClipboardEngine:
    """Reads and writes the real OS clipboard."""

    def __init__(self) -> None:
        self._platform = HostPlatform()
        self._available = self._probe()

    def _probe(self) -> bool:
        if not self._platform.is_supported or self._platform.is_linux:
            # pyperclip's Linux backend depends on a GUI clipboard tool
            # (xclip/xsel/wl-copy) that we cannot guarantee; treat as
            # unavailable to fail honestly on headless hosts.
            if self._platform.is_linux:
                return False
            if not self._platform.is_supported:
                return False
        try:
            import pyperclip  # noqa: F401

            return True
        except ImportError:
            return False

    def read(self) -> ClipboardResult:
        """Read the current clipboard text."""
        result = ClipboardResult(operation="read")
        if not self._available:
            result.error = "Clipboard backend not available on this platform."
            return result
        try:
            import pyperclip

            text = pyperclip.paste()
            result.content = text or ""
            result.length = len(result.content)
            logger.info("Read clipboard (%d chars)", result.length)
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Clipboard read failed")
        return result

    def write(self, text: str) -> ClipboardResult:
        """Replace the clipboard contents with ``text``."""
        result = ClipboardResult(operation="write")
        if not self._available:
            result.error = "Clipboard backend not available on this platform."
            return result
        try:
            import pyperclip

            pyperclip.copy(text)
            result.length = len(text)
            logger.info("Wrote clipboard (%d chars)", result.length)
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Clipboard write failed")
        return result

    @property
    def is_available(self) -> bool:
        return self._available


@lru_cache
def get_clipboard_engine() -> ClipboardEngine:
    """Return the process-wide clipboard engine (shared by API + agent tools)."""
    return ClipboardEngine()
