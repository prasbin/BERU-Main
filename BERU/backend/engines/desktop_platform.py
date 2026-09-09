"""Desktop capability detection and platform gating.

Centralises host-OS detection and per-capability availability so that the
desktop engines (screenshot, mouse, keyboard, clipboard, applications) can
report a safe ``unavailable`` state on platforms they do not support, rather
than raising or faking success.

All platform-specific branching lives through this module so the rest of the
desktop stack stays portable and easy to extend (Windows today, Linux/macOS
tomorrow).
"""

from __future__ import annotations

import platform


class HostPlatform:
    """Detect the host OS once and expose stable predicates."""

    def __init__(self, system: str | None = None) -> None:
        self._system = (system or platform.system()).lower()

    @property
    def name(self) -> str:
        return self._system

    @property
    def is_windows(self) -> bool:
        return self._system == "windows"

    @property
    def is_linux(self) -> bool:
        return self._system == "linux"

    @property
    def is_macos(self) -> bool:
        return self._system == "darwin"

    @property
    def is_supported(self) -> bool:
        """Whether this host platform is a supported desktop target."""
        return self._system in ("windows", "linux", "darwin")
