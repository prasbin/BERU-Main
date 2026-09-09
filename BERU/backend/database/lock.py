"""Single-instance database lock.

BERU keeps its durable state (schema + data) in one database file and runs
Alembic migrations at startup. Two processes must never migrate the same
database at the same time, so each process takes an exclusive lock file next to
the database before starting. The lock is released on clean shutdown; a stale
lock left by a crashed process is reaped automatically.

Locking is file-backed SQLite only: ``:memory:`` databases cannot be shared
across processes anyway, and multi-process servers expose the database through
their own concurrency controls.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger(__name__)


class DatabaseLockError(RuntimeError):
    """Raised when another BERU instance holds the database lock."""


class DatabaseLock:
    """An exclusive owner token represented by a lock file's existence."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._active = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        """Take the lock, reaping a stale lock left by a dead process."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):  # one retry after reaping a stale lock
            try:
                fd = os.open(
                    self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                owner = _read_owner(self._path)
                if owner is None or _pid_is_alive(owner):
                    raise DatabaseLockError(
                        f"Another BERU instance already holds the database lock "
                        f"({self._path})"
                    ) from None
                logger.warning(
                    "Reaping stale database lock %s (owner pid %s is gone)",
                    self._path,
                    owner,
                )
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    pass
            else:
                with os.fdopen(fd, "w") as handle:
                    handle.write(str(os.getpid()))
                self._active = True
                return
        raise DatabaseLockError(
            f"Could not acquire database lock {self._path}"
        )

    def release(self) -> None:
        """Drop the lock so another instance may start."""
        if not self._active:
            return
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        self._active = False


def database_lock_path(database_url: str) -> Path | None:
    """Return the lock-file path for a database URL, or ``None`` when no lock is
    needed (in-memory or non-file databases)."""
    if not database_url.startswith("sqlite"):
        return None
    body = unquote(database_url.split("://", 1)[1].lstrip("/"))
    if body == ":memory:" or body.startswith(":memory:"):
        return None
    path = Path(body)
    return path.with_name(path.name + ".beru.lock")


def acquire_db_lock(database_url: str) -> DatabaseLock | None:
    """Acquire the single-instance lock for ``database_url`` (``None`` when the
    database needs no lock)."""
    path = database_lock_path(database_url)
    if path is None:
        return None
    lock = DatabaseLock(path)
    lock.acquire()
    return lock


def _read_owner(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows
        try:
            import ctypes

            # PROCESS_QUERY_LIMITED_INFORMATION — open without killing.
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 - probe failures mean "treat as alive"
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True