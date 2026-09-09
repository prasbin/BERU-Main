"""App launcher — launches applications on the host system.

Provides a controlled interface for starting applications by name or path,
with process tracking and platform-specific handling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

#: Looks like a URI scheme ("ms-settings:", "mailto:", ...) — launched with the
#: OS default handler (``os.startfile`` on Windows) instead of a subprocess.
#: A colon followed by a path separator (``C:\\...``, ``C:/...``) is a Windows
#: drive-letter path, not a scheme.
_URI_RE = re.compile(r"^[a-z][a-z0-9+.-]*:(?![\\/])", re.IGNORECASE)


class LaunchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    NOT_FOUND = "not_found"


@dataclass
class LaunchResult:
    """Result of an app launch."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    app_name: str = ""
    command: str = ""
    status: LaunchStatus = LaunchStatus.PENDING
    pid: int | None = None
    error: str | None = None
    launched_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "app_name": self.app_name,
            "command": self.command,
            "status": self.status.value,
            "pid": self.pid,
        }
        if self.error:
            result["error"] = self.error
        return result


class AppLauncher:
    """Launches applications with platform-specific handling.

    Launching never goes through a shell: executables are started via
    ``Popen`` with an argv list (so no metacharacter or quoting-injection
    surface exists), and URI-style targets (``ms-settings:``) are handed to
    the OS default handler. User-supplied arguments are passed positionally,
    never string-concatenated into a command line that a shell could reparse.
    """

    # Common app names to argv lists per platform. These are trusted constants
    # (never split from user input); arguments are appended afterwards.
    APP_ALIASES: dict[str, dict[str, list[str]]] = {
        "windows": {
            "notepad": ["notepad.exe"],
            "calculator": ["calc.exe"],
            "paint": ["mspaint.exe"],
            "explorer": ["explorer.exe"],
            "terminal": ["cmd.exe"],
            "powershell": ["powershell.exe"],
            "settings": ["ms-settings:"],
        },
        "darwin": {
            "finder": ["open", "-a", "Finder"],
            "safari": ["open", "-a", "Safari"],
            "terminal": ["open", "-a", "Terminal"],
            "calculator": ["open", "-a", "Calculator"],
            "textedit": ["open", "-a", "TextEdit"],
            "settings": ["open", "-a", "System Settings"],
        },
        "linux": {
            "terminal": ["x-terminal-emulator"],
            "files": ["xdg-open", "."],
            "browser": ["xdg-open"],
            "settings": ["gnome-control-center"],
            "calculator": ["gnome-calculator"],
            "text-editor": ["gnome-text-editor"],
        },
    }

    def __init__(self) -> None:
        self._platform = platform.system().lower()
        self._launched: dict[str, LaunchResult] = {}
        self._aliases = self.APP_ALIASES.get(self._platform, {})

    def resolve_command(self, app_name: str) -> str:
        """Resolve an app name to its argv list joined for display."""
        return " ".join(self.resolve_argv(app_name))

    def resolve_argv(self, app_name: str) -> list[str]:
        """Resolve an app name to a launch argv list (never through a shell)."""
        # Check aliases first; aliases are trusted constants, return copies.
        if app_name.lower() in self._aliases:
            return list(self._aliases[app_name.lower()])

        # Explicit paths are launched verbatim, without any shell parsing.
        if "/" in app_name or "\\" in app_name:
            return [app_name]

        # Platform-specific defaults.
        if self._platform == "darwin":
            return ["open", "-a", app_name]
        return [app_name]

    @staticmethod
    def _is_uri(target: str) -> bool:
        return bool(_URI_RE.match(target))

    async def launch(
        self,
        app_name: str,
        args: list[str] | None = None,
        cwd: str | None = None,
    ) -> LaunchResult:
        """Launch an application."""
        result = LaunchResult(app_name=app_name)
        argv = self.resolve_argv(app_name)
        if args:
            argv = [*argv, *args]
        result.command = " ".join(argv)
        result.launched_at = datetime.now(timezone.utc)

        target = argv[0] if argv else ""
        try:
            # URI schemes (ms-settings:, mailto:, ...) go to the OS default
            # handler; subprocess launching them would need a shell.
            if self._is_uri(target):
                if self._platform != "windows":
                    argv = ["xdg-open" if self._platform == "linux" else "open", target]
                else:
                    await asyncio.to_thread(os.startfile, target)
                    return self._mark_launched(result, pid=None)

            if self._platform == "windows":
                # CREATE_NEW_PROCESS_GROUP keeps the app detached from the
                # console without a shell intermediary.
                process = subprocess.Popen(
                    argv if len(argv) > 1 else argv[0],
                    shell=False,
                    cwd=cwd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = subprocess.Popen(
                    argv,
                    shell=False,
                    cwd=cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            return self._mark_launched(result, pid=process.pid)

        except FileNotFoundError:
            result.status = LaunchStatus.NOT_FOUND
            result.error = f"Application not found: {app_name}"
        except Exception as exc:
            result.status = LaunchStatus.FAILED
            result.error = str(exc)
            logger.exception("Failed to launch '%s'", app_name)

        return result

    def _mark_launched(self, result: LaunchResult, *, pid: int | None) -> LaunchResult:
        result.pid = pid
        result.status = LaunchStatus.RUNNING
        self._launched[result.id] = result
        logger.info("Launched '%s' (pid=%s)", result.app_name, pid)
        return result

    def get_launched(self, launch_id: str) -> LaunchResult | None:
        return self._launched.get(launch_id)

    def list_launched(self) -> list[LaunchResult]:
        return list(self._launched.values())


@lru_cache
def get_app_launcher() -> AppLauncher:
    """Return the process-wide app launcher (shared by API + agent tools)."""
    return AppLauncher()
