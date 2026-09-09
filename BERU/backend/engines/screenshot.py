"""Screenshot engine — captures the real desktop screen.

Uses ``mss`` for a fast full-screen grab and ``Pillow`` to compute basic image
metadata (size, dominant colour, brightness) that gives BERU a lightweight
sense of the current screen state. The raw PNG is written to a temp directory
so it can be surfaced to the user or an agent.

If the screenshot libraries are not installed, the engine reports an
``unavailable`` state instead of crashing, satisfying the "safe disabled"
constraint.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.engines.desktop_platform import HostPlatform

logger = logging.getLogger(__name__)


@dataclass
class ScreenshotResult:
    """Result of a desktop screenshot capture."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    path: str = ""
    width: int = 0
    height: int = 0
    dominant_color: str = ""
    average_brightness: float = 0
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "dominant_color": self.dominant_color,
            "average_brightness": round(self.average_brightness, 3),
            "captured_at": self.captured_at.isoformat(),
        }
        if self.error:
            result["error"] = self.error
        return result


class ScreenshotEngine:
    """Captures real screenshots of the host desktop."""

    def __init__(self, save_dir: str | Path | None = None) -> None:
        self._platform = HostPlatform()
        self._save_dir = Path(save_dir or self._default_save_dir())
        self._save_dir.mkdir(parents=True, exist_ok=True)
        self._available = self._probe()

    @staticmethod
    def _default_save_dir() -> Path:
        import tempfile

        base = Path(tempfile.gettempdir()) / "beru_screenshots"
        return base

    def _probe(self) -> bool:
        """Return True if the screenshot backend can be loaded."""
        if not self._platform.is_supported:
            return False
        try:
            import mss  # noqa: F401
            from PIL import Image  # noqa: F401

            return True
        except ImportError:
            return False

    def _hex_color(self, r: int, g: int, b: int) -> str:
        return f"#{r:02x}{g:02x}{b:02x}"

    def capture(self) -> ScreenshotResult:
        """Capture the full screen and write it to the save directory."""
        result = ScreenshotResult()
        if not self._available:
            result.error = "Screenshot backend not available on this platform."
            return result

        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                monitor = sct.monitors[0]  # full virtual screen
                raw = sct.grab(monitor)
                png = mss.tools.to_png(raw.rgb, raw.size)
                image = Image.frombytes("RGB", raw.size, raw.rgb)

            stamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{stamp}_{result.id}.png"
            filepath = self._save_dir / filename
            filepath.write_bytes(png)

            # Basic screen-state metadata.
            thumb = image.convert("RGB").resize((64, 64))
            pixels = list(thumb.getdata())
            n = max(len(pixels), 1)
            r = sum(p[0] for p in pixels) // n
            g = sum(p[1] for p in pixels) // n
            b = sum(p[2] for p in pixels) // n
            brightness = (r + g + b) / 3 / 255.0

            result.path = str(filepath)
            result.width, result.height = raw.size
            result.dominant_color = self._hex_color(r, g, b)
            result.average_brightness = brightness
            logger.info(
                "Captured screenshot %s (%dx%d)",
                result.path,
                result.width,
                result.height,
            )
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Screenshot capture failed")

        return result

    @property
    def is_available(self) -> bool:
        return self._available


@lru_cache
def get_screenshot_engine() -> ScreenshotEngine:
    """Return the process-wide screenshot engine (shared by API + agent tools)."""
    return ScreenshotEngine()
