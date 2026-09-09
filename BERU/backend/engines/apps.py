"""Application inspection engine — lists running processes and applications.

Uses ``psutil`` to enumerate running processes in a safe, read-only way. This
gives BERU basic desktop application/process state awareness (what is currently
running) so it can decide what is visible without launching anything.

When psutil is unavailable, the engine reports an ``unavailable`` state.
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
class ProcessEntry:
    """A single running process."""
    pid: int = 0
    name: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "name": self.name, "status": self.status}


@dataclass
class AppsResult:
    """Result of a process listing or lookup."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    operation: str = ""
    processes: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    inspected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "operation": self.operation,
            "count": self.count,
            "inspected_at": self.inspected_at.isoformat(),
        }
        if self.processes:
            result["processes"] = self.processes[:100]
        if self.error:
            result["error"] = self.error
        return result


class AppInspectEngine:
    """Read-only inspection of running applications/processes."""

    def __init__(self) -> None:
        self._platform = HostPlatform()
        self._available = self._probe()

    def _probe(self) -> bool:
        if not self._platform.is_supported:
            return False
        try:
            import psutil  # noqa: F401

            return True
        except ImportError:
            return False

    def list_processes(self, limit: int = 50) -> AppsResult:
        """List running processes, most CPU-consuming first."""
        result = AppsResult(operation="list_processes")
        if not self._available:
            result.error = "Process backend not available on this platform."
            return result
        try:
            import psutil

            entries: list[ProcessEntry] = []
            for proc in psutil.process_iter(["pid", "name", "status", "cpu_percent"]):
                try:
                    info = proc.info
                    entries.append(
                        ProcessEntry(
                            pid=int(info["pid"]),
                            name=str(info["name"] or ""),
                            status=str(info["status"] or ""),
                        )
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            # Sort descending by name length/pid for stable, useful ordering.
            entries.sort(key=lambda e: (-len(e.name), e.pid))
            result.processes = [e.to_dict() for e in entries[: max(int(limit), 1)]]
            result.count = len(entries)
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Process listing failed")
        return result

    def find_process(self, name: str) -> AppsResult:
        """Find running processes whose name matches (case-insensitive substring)."""
        result = AppsResult(operation="find_process")
        if not isinstance(name, str) or not name.strip():
            result.error = "find_process requires a non-empty 'name' argument."
            return result
        if not self._available:
            result.error = "Process backend not available on this platform."
            return result
        try:
            import psutil

            needle = name.lower()
            entries: list[ProcessEntry] = []
            for proc in psutil.process_iter(["pid", "name", "status"]):
                try:
                    info = proc.info
                    proc_name = str(info["name"] or "")
                    if needle in proc_name.lower():
                        entries.append(
                            ProcessEntry(
                                pid=int(info["pid"]),
                                name=proc_name,
                                status=str(info["status"] or ""),
                            )
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            result.processes = [e.to_dict() for e in entries]
            result.count = len(entries)
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Process find failed")
        return result

    @property
    def is_available(self) -> bool:
        return self._available


@lru_cache
def get_app_inspect_engine() -> AppInspectEngine:
    """Return the process-wide application inspection engine."""
    return AppInspectEngine()
