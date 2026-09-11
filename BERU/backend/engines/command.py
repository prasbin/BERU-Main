"""Command executor — runs system commands with sandboxing and safety controls.

Provides a controlled interface for executing shell commands, with allowlists,
timeout enforcement, output capture, and audit logging. Commands run in a
subprocess with restricted capabilities.

Host hardening (Stage 5.4):

* **Least-privilege account** — when ``BERU_COMMAND_RUN_USER`` /
  ``BERU_COMMAND_RUN_GROUP`` are set, POSIX hosts drop privileges for the child
  (``setuid``). Windows cannot drop privileges through a stdlib subprocess, so
  a configured account is refused with a ``BLOCKED`` result rather than
  silently ignored — use the container backend there.
* **Container backend** — :class:`backend.engines.container.ContainerCommandExecutor`
  runs the same guardrail layer (allowlist + pattern-blocklist) and executes
  via ``docker exec``, so destructive commands are blocked in every backend.

The allowlist + pattern-blocklist guardrail remains the primary defense and is
identical across backends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from typing import Any

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

# Bound on the in-memory command history ring buffer (observability, not
# authority — prevents unbounded growth over a long-lived process).
_MAX_HISTORY = 200


def _normalize_command(command: str) -> str:
    """Collapse all whitespace and lower-case, so blocklist matching cannot be
    defeated with tabs, extra spaces, or case tricks (``RM   -rF`` == ``rm -rf``)."""
    return " ".join(command.split()).lower()


def _collect_flags(tokens: list[str], start: int = 1) -> set[str]:
    """Collect short/long flags from a token list (``-rf``, ``--recursive``, ``/s``)."""
    flags: set[str] = set()
    for tok in tokens[start:]:
        if tok.startswith("--"):
            flags.update(tok[2:].split())
        elif tok.startswith("-") and len(tok) > 1 and not tok[1:].isdigit():
            flags.update(tok[1:].lower())
        elif tok.startswith("/") and len(tok) > 1 and not tok[1:].isdigit():
            flags.update(tok[1:].lower())
    return flags


_STANDALONE_DESTRUCTIVE = {
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "mkfs",
    "fdisk",
    "diskpart",
    "format",
}


def _is_destructively_recursive(normalized: str) -> str | None:
    """Detect recursive+force wipes that endanger the whole filesystem.

    Catches real invocations (``rm -rf /important``, ``del /s /q C:\\``,
    ``Remove-Item -Recurse -Force ~``) whatever their spacing or casing, since
    shells keep the semantics even when whitespace is mangled.
    """
    tokens = normalized.split()
    flags = _collect_flags(tokens)

    if "rm" in tokens:
        rm_flags = _collect_flags(tokens, tokens.index("rm") + 1)
        if "r" in rm_flags and ("f" in rm_flags or "force" in rm_flags):
            return "Refusing to run a recursive force delete ('rm -rf')."
    if "rmdir" in tokens or "rd" in tokens:
        idx = tokens.index("rmdir") if "rmdir" in tokens else tokens.index("rd")
        if "s" in _collect_flags(tokens, idx + 1):
            return "Refusing to run a recursive directory delete ('rd /s')."
    if tokens and tokens[0].startswith(("del", "erase")):
        if "s" in flags:
            return "Refusing to run a recursive delete ('del /s')."
    if "remove-item" in normalized:
        has_recurse = "recurse" in normalized
        has_force = "force" in normalized or normalized.endswith(" -f")
        if has_recurse and has_force:
            return "Refusing to run a recursive force Remove-Item."
    if "git" in tokens and "clean" in tokens:
        return "Refusing to run 'git clean' (destructive untracked-file removal)."
    if "clear-content" in normalized:
        return "Refusing to run 'Clear-Content'."
    return None


def _is_host_destructive(normalized: str) -> str | None:
    """Detect commands that power off/reset or reformat the machine."""
    tokens = normalized.split()
    if not tokens:
        return None
    if tokens[0].startswith("dd") and "of=/dev" in normalized:
        return "Refusing to run 'dd' targeting a block device."
    head = tokens[0]
    if any(head == d or head.startswith(f"{d}.") for d in _STANDALONE_DESTRUCTIVE):
        return f"Refusing to run '{head}' (destructive system command)."
    return None


class CommandStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


@dataclass
class CommandResult:
    """Result of a command execution."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    command: str = ""
    status: CommandStatus = CommandStatus.PENDING
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float = 0
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "command": self.command,
            "status": self.status.value,
            "stdout": self.stdout[:5000],  # Truncate large output
            "stderr": self.stderr[:2000],
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
        }
        if self.blocked_reason:
            result["blocked_reason"] = self.blocked_reason
        return result


class CommandExecutor:
    """Executes system commands with sandboxing and safety controls."""

    # Default blocked commands (dangerous destructive operations)
    DEFAULT_BLOCKED = [
        "rm -rf /",
        "mkfs",
        ":(){ :|:& };:",  # fork bomb
        "dd if=/dev/zero of=/dev/sda",
    ]

    # Safe commands that don't require confirmation
    SAFE_COMMANDS = [
        "ls", "pwd", "echo", "date", "whoami", "hostname",
        "cat", "head", "tail", "wc", "grep", "find",
        "python", "pip", "node", "npm",
        "git status", "git log", "git diff",
    ]

    def __init__(
        self,
        allowed_commands: list[str] | None = None,
        blocked_commands: list[str] | None = None,
        timeout_seconds: float = 30,
        max_output_bytes: int = 1024 * 1024,  # 1MB
        *,
        user: str | None = None,
        group: str | None = None,
    ) -> None:
        self._allowed = allowed_commands
        self._blocked = blocked_commands or self.DEFAULT_BLOCKED
        self._timeout = timeout_seconds
        self._max_output = max_output_bytes
        self._history: list[CommandResult] = []
        self._platform = platform.system().lower()
        self._user = user
        self._group = group

    def _is_blocked(self, command: str) -> str | None:
        """Check if a command is blocked. Returns block reason or None."""
        normalized = _normalize_command(command)
        for blocked in self._blocked:
            if normalized and str(blocked).lower().strip() in normalized:
                return f"Command contains blocked pattern: '{blocked}'"
        if normalized:
            reason = _is_destructively_recursive(normalized)
            if reason:
                return reason
            reason = _is_host_destructive(normalized)
            if reason:
                return reason
        return None

    def _is_allowed(self, command: str) -> bool:
        """Check if a command is in the allowlist (if one is configured)."""
        if self._allowed is None:
            return True  # No allowlist = everything allowed
        cmd_base = command.strip().split()[0] if command.strip() else ""
        return cmd_base in self._allowed or command.strip() in self._allowed

    def _append(self, result: CommandResult) -> None:
        """Record a result, keeping the history ring buffer bounded."""
        self._history.append(result)
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]

    def _build_shell_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Build the appropriate shell command for the platform.

        ``cwd`` and ``env`` are accepted for override parity with container
        backends; the host backend applies them via ``create_subprocess_exec``
        kwargs rather than encoding them in the argv list.
        """
        if self._platform == "windows":
            return ["cmd", "/c", command]
        else:
            return ["bash", "-c", command]

    async def execute(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Execute a system command asynchronously."""
        result = CommandResult(command=command)

        # Check if blocked
        block_reason = self._is_blocked(command)
        if block_reason:
            result.status = CommandStatus.BLOCKED
            result.blocked_reason = block_reason
            logger.warning("Blocked command: %s — %s", command, block_reason)
            self._append(result)
            return result

        # Check if allowed
        if not self._is_allowed(command):
            result.status = CommandStatus.BLOCKED
            result.blocked_reason = f"Command not in allowlist: '{command}'"
            self._append(result)
            return result

        # Least-privilege enforcement
        effective_user = self._user
        effective_group = self._group
        if (effective_user or effective_group) and self._platform == "windows":
            result.status = CommandStatus.BLOCKED
            result.blocked_reason = (
                "Least-privilege execution (BERU_COMMAND_RUN_USER/GROUP) is not supported "
                "on Windows host subprocesses; use BERU_COMMAND_EXECUTOR=container for "
                "isolated execution."
            )
            self._append(result)
            return result

        result.status = CommandStatus.RUNNING
        result.started_at = datetime.now(timezone.utc)

        effective_timeout = timeout or self._timeout
        shell_cmd = self._build_shell_command(command, cwd=cwd, env=env)

        try:
            subprocess_kwargs: dict[str, Any] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
            }
            if cwd:
                subprocess_kwargs["cwd"] = cwd
            if env:
                subprocess_kwargs["env"] = {**os.environ, **env}
            if self._platform != "windows":
                if effective_user:
                    subprocess_kwargs["user"] = effective_user
                if effective_group:
                    subprocess_kwargs["group"] = effective_group

            process = await asyncio.create_subprocess_exec(
                *shell_cmd,
                **subprocess_kwargs,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                result.status = CommandStatus.TIMEOUT
                result.stderr = f"Command timed out after {effective_timeout}s"
                result.completed_at = datetime.now(timezone.utc)
                self._append(result)
                return result

            result.stdout = stdout_bytes.decode("utf-8", errors="replace")[:self._max_output]
            result.stderr = stderr_bytes.decode("utf-8", errors="replace")[:self._max_output]
            result.exit_code = process.returncode
            result.status = (
                CommandStatus.COMPLETED if process.returncode == 0 else CommandStatus.FAILED
            )

        except Exception as exc:
            result.status = CommandStatus.FAILED
            result.stderr = str(exc)

        result.completed_at = datetime.now(timezone.utc)
        if result.started_at and result.completed_at:
            delta = result.completed_at - result.started_at
            result.duration_ms = delta.total_seconds() * 1000

        self._append(result)
        logger.info(
            "Command [%s]: %s (exit=%s, %.0fms)",
            result.status.value,
            command,
            result.exit_code,
            result.duration_ms,
        )
        return result

    def get_history(self, limit: int = 50) -> list[CommandResult]:
        return self._history[-limit:]

    def clear_history(self) -> int:
        count = len(self._history)
        self._history.clear()
        return count


@lru_cache
def get_command_executor() -> CommandExecutor:
    """Return the process-wide command executor (shared by API + agent tools).

    The backend is selected from ``BERU_COMMAND_EXECUTOR``:

    * ``"host"`` (default) — local subprocess with the allowlist +
      pattern-blocklist guardrail. Honours the optional
      ``BERU_COMMAND_RUN_USER`` / ``BERU_COMMAND_RUN_GROUP`` (POSIX only;
      Windows refuses this with a ``BLOCKED`` result and requires the
      container backend).
    * ``"container"`` — wraps the guardrail around ``docker exec`` into the
      name given by ``BERU_COMMAND_CONTAINER``.
    """
    settings = get_settings()
    if settings.command_executor == "container":
        container = settings.command_container.strip()
        if not container:
            logger.warning(
                "BERU_COMMAND_EXECUTOR=container but BERU_COMMAND_CONTAINER is "
                "empty; using the host executor."
            )
        else:
            from backend.engines.container import ContainerCommandExecutor

            return ContainerCommandExecutor(container=container)
    return CommandExecutor(
        user=settings.command_run_user or None,
        group=settings.command_run_group or None,
    )
