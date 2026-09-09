"""OS notification system — sends notifications to the host desktop.

Provides a cross-platform interface for sending desktop notifications,
with fallback to in-app notifications when OS notifications aren't available.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Bound on the in-memory notification history ring buffer.
_MAX_HISTORY = 200


def _ps_quote(text: str) -> str:
    """Embed ``text`` as a PowerShell single-quoted string literal."""
    return "'" + text.replace("'", "''") + "'"


def _as_quote(text: str) -> str:
    """Embed ``text`` as an AppleScript string literal (no multiline)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = " ".join(escaped.split())  # AppleScript has no literal newlines
    return f'"{escaped}"'


@dataclass
class OSNotification:
    """An OS-level notification."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    message: str = ""
    app_name: str = "BERU"
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    delivered: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "app_name": self.app_name,
            "sent_at": self.sent_at.isoformat(),
            "delivered": self.delivered,
        }
        if self.error:
            result["error"] = self.error
        return result


class OSNotifier:
    """Sends desktop notifications across platforms."""

    def __init__(self) -> None:
        self._platform = platform.system().lower()
        self._history: list[OSNotification] = []
        self._available = self._check_availability()

    def _check_availability(self) -> bool:
        """Check if OS notifications are available."""
        try:
            if self._platform == "windows":
                # PowerShell toast notifications
                check_cmd = (
                    "Get-Command New-BurntToastNotification "
                    "-ErrorAction SilentlyContinue"
                )
                result = subprocess.run(
                    ["powershell", "-Command", check_cmd],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            elif self._platform == "darwin":
                # macOS notifications via osascript
                result = subprocess.run(
                    ["osascript", "-e", 'display notification "test" with title "test"'],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            elif self._platform == "linux":
                # Check for notify-send
                result = subprocess.run(
                    ["which", "notify-send"],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
        except Exception:
            pass
        return False

    async def send(
        self,
        title: str,
        message: str,
        app_name: str = "BERU",
    ) -> OSNotification:
        """Send a desktop notification."""
        notif = OSNotification(title=title, message=message, app_name=app_name)

        def _append(n: OSNotification) -> None:
            self._history.append(n)
            if len(self._history) > _MAX_HISTORY:
                self._history = self._history[-_MAX_HISTORY:]

        if not self._available:
            notif.delivered = False
            notif.error = "OS notifications not available on this platform"
            _append(notif)
            return notif

        try:
            if self._platform == "windows":
                await self._send_windows(title, message, app_name)
            elif self._platform == "darwin":
                await self._send_macos(title, message, app_name)
            elif self._platform == "linux":
                await self._send_linux(title, message, app_name)
            notif.delivered = True
        except Exception as exc:
            notif.delivered = False
            notif.error = str(exc)
            logger.exception("Failed to send OS notification")

        _append(notif)
        return notif

    async def _send_windows(self, title: str, message: str, app_name: str) -> None:
        """Send notification on Windows using PowerShell.

        ``title``/``message`` are embedded as single-quoted PowerShell string
        literals with ``'`` doubled, so malicious quoting in either field can
        never terminate the literal and reach the rest of the script.
        """
        manager = "Windows.UI.Notifications.ToastNotificationManager"
        toast_type = "Windows.UI.Notifications.ToastTemplateType"
        runtime = "Windows.UI.Notifications, ContentType = WindowsRuntime"

        ps_script = f"""
        [{manager}, {runtime}] | Out-Null
        $template = [{manager}]::GetTemplateContent([{toast_type}]::ToastText02)
        $text = $template.GetElementsByTagName('text')
        $text[0].AppendChild($template.CreateTextNode({_ps_quote(title)})) | Out-Null
        $text[1].AppendChild($template.CreateTextNode({_ps_quote(message)})) | Out-Null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        """
        process = await asyncio.create_subprocess_exec(
            "powershell", "-Command", ps_script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

    async def _send_macos(self, title: str, message: str, app_name: str) -> None:
        """Send notification on macOS using osascript.

        ``title``/``message``/``app_name`` are embedded as AppleScript string
        literals with ``"`` and ``\\`` escaped, so no field can break out of the
        literal or inject additional osascript statements.
        """
        script = (
            f'display notification {_as_quote(message)} with title {_as_quote(title)} '
            f'subtitle {_as_quote(app_name)}'
        )
        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

    async def _send_linux(self, title: str, message: str, app_name: str) -> None:
        """Send notification on Linux using notify-send."""
        process = await asyncio.create_subprocess_exec(
            "notify-send", "--app-name", app_name, title, message,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

    def get_history(self, limit: int = 50) -> list[OSNotification]:
        return self._history[-limit:]

    def clear_history(self) -> int:
        count = len(self._history)
        self._history.clear()
        return count

    @property
    def is_available(self) -> bool:
        return self._available


@lru_cache
def get_os_notifier() -> OSNotifier:
    """Return the process-wide OS notifier (shared by API + agent tools)."""
    return OSNotifier()
