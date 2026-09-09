"""Single-instance database lock tests.

The lock exists so two BERU processes can never run Alembic migrations against
the same SQLite file at once. It must be exclusive, reaped when the owner died,
and released on clean shutdown.
"""

from __future__ import annotations

import pytest


def test_lock_paths_for_urls():
    from backend.database.lock import database_lock_path

    p = database_lock_path("sqlite+aiosqlite:///./beru.db")
    assert p is not None
    assert p.name == "beru.db.beru.lock"
    assert database_lock_path("sqlite+aiosqlite:///:memory:") is None
    assert database_lock_path("sqlite:///C:/tmp/db.sqlite3").name == "db.sqlite3.beru.lock"
    assert database_lock_path("postgresql://user:pass@host/db") is None


def test_lock_is_exclusive_until_released(tmp_path):
    from backend.database.lock import DatabaseLock, DatabaseLockError

    first = DatabaseLock(tmp_path / "db.lock")
    first.acquire()

    second = DatabaseLock(tmp_path / "db.lock")
    with pytest.raises(DatabaseLockError, match="already holds"):
        second.acquire()

    first.release()
    second.acquire()
    second.release()


def test_stale_lock_from_dead_process_is_reaped(tmp_path):
    from backend.database.lock import DatabaseLock

    lock_path = tmp_path / "db.lock"
    lock_path.write_text("2147483647")  # a pid no real process can own
    lock = DatabaseLock(lock_path)
    lock.acquire()
    assert lock_path.exists()
    assert lock_path.read_text().strip() == str(__import__("os").getpid())
    lock.release()
    assert not lock_path.exists()