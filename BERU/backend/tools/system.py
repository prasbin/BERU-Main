"""System control tools for agents.

Tools that allow agents to run commands, launch apps, and send notifications.
These tools execute against the real host engines (:class:`CommandExecutor`,
:class:`AppLauncher`, :class:`OSNotifier`) and require explicit confirmation,
as they interact with the host OS.

All engine instances come from the process-wide singletons, so an agent-driven
run and a direct API call share the same execution history.
"""

from __future__ import annotations

import platform
from typing import Any

from backend.engines.app_launcher import get_app_launcher
from backend.engines.command import CommandStatus, get_command_executor
from backend.engines.notifier import get_os_notifier
from backend.tools.base import Tool, ToolResult


class RunCommandTool(Tool):
    """Run a shell command on the host system."""

    name = "run_command"
    description = "Run a shell command on the host system with sandboxing."
    permissions = ["execute"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "number",
                "description": "Maximum execution time in seconds.",
            },
        },
        "required": ["command"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command", "")
        timeout = kwargs.get("timeout")
        if not isinstance(command, str):
            return ToolResult.failure("run_command requires a 'command' string argument.")
        if not command.strip():
            return ToolResult.failure("run_command requires a 'command' argument.")

        result = await get_command_executor().execute(command=command, timeout=timeout)

        if result.status == CommandStatus.BLOCKED:
            return ToolResult.failure(result.blocked_reason or "Command blocked.")
        if result.status == CommandStatus.TIMEOUT:
            return ToolResult.failure(f"Command timed out: {result.stderr or ''}".strip())
        if result.status == CommandStatus.FAILED:
            detail = (result.stderr or result.stdout or "").strip()
            return ToolResult.failure(
                f"Command failed (exit={result.exit_code}): {detail}"
            )

        return ToolResult.success(result.to_dict())


class LaunchAppTool(Tool):
    """Launch an application on the host system."""

    name = "launch_app"
    description = "Launch an application by name on the host system."
    permissions = ["execute"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Name of the application to launch.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional arguments to pass to the app.",
            },
        },
        "required": ["app_name"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        app_name = kwargs.get("app_name", "")
        args = kwargs.get("args") or []
        if not app_name:
            return ToolResult.failure("launch_app requires an 'app_name' argument.")

        result = await get_app_launcher().launch(app_name=app_name, args=list(args))

        if result.status.value in ("not_found", "failed"):
            return ToolResult.failure(
                result.error or f"Failed to launch application '{app_name}'."
            )

        return ToolResult.success(result.to_dict())


class SendNotificationTool(Tool):
    """Send a desktop notification."""

    name = "send_notification"
    description = "Send a desktop notification to the user."
    permissions = ["notify"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Notification title.",
            },
            "message": {
                "type": "string",
                "description": "Notification message body.",
            },
        },
        "required": ["title", "message"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        title = kwargs.get("title", "")
        message = kwargs.get("message", "")

        notification = await get_os_notifier().send(title=title, message=message)

        if not notification.delivered:
            reason = notification.error or "OS notifications not available."
            return ToolResult.failure(f"Notification failed: {reason}")

        return ToolResult.success(notification.to_dict())


class GetSystemInfoTool(Tool):
    """Get information about the host system."""

    name = "get_system_info"
    description = "Get information about the host system (OS, arch, platform)."
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success(
            {
                "system": platform.system(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "release": platform.release(),
                "python_version": platform.python_version(),
            }
        )