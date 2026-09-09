"""Tests for the SQLite backup / restore service."""

from __future__ import annotations

import sqlite3

import pytest

from backend.models.conversation import Conversation
from backend.services.backup import (
    _compute_table_counts,
    _main,
    backup_database,
    resolve_sqlite_path,
    restore_database,
    verify_backup,
    verify_db,
)


def _seed(path, tables=("conversations", "activity_records"), rows=3):
    """Create a small SQLite db at ``path`` with a couple of tables + rows."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        for name in tables:
            con.execute(
                f"CREATE TABLE {name} (id TEXT PRIMARY KEY, value TEXT)"
            )
            for i in range(rows):
                con.execute(f"INSERT INTO {name} VALUES (?, ?)", (f"{name}-{i}", f"v{i}"))
        con.commit()
    finally:
        con.close()
    return path


# ---- path resolution ----


def test_resolve_sqlite_path_forms():
    assert resolve_sqlite_path("sqlite+aiosqlite:///./beru.db").name == "beru.db"
    assert resolve_sqlite_path("sqlite:///./beru.db").name == "beru.db"
    p = resolve_sqlite_path("sqlite:////tmp/x/beru.db")
    assert str(p).replace("\\", "/") == "/tmp/x/beru.db"

    with_backend = resolve_sqlite_path("sqlite+aiosqlite:///./app.db?check_same_thread=False")
    assert with_backend.name == "app.db"


def test_resolve_sqlite_path_rejects_unsupported():
    with pytest.raises(ValueError):
        resolve_sqlite_path("postgresql+asyncpg://user:pw@localhost/app")
    with pytest.raises(ValueError):
        resolve_sqlite_path("mysql+pymysql://user:pw@localhost/app")
    with pytest.raises(ValueError):
        resolve_sqlite_path("sqlite:///:memory:")


# ---- snapshot / verification ----


def test_backup_database_creates_verified_snapshot(tmp_path):
    source = _seed(tmp_path / "beru.db")
    target = backup_database(database_url=f"sqlite+aiosqlite:///{source}")

    assert target.exists()
    # Byte-for-byte row parity and clean integrity on both files.
    verify_db(source)
    assert _compute_table_counts(source) == _compute_table_counts(target)
    assert _compute_table_counts(target) == {
        "activity_records": 3,
        "conversations": 3,
    }


def test_backup_default_target_is_timestamped_sibling(tmp_path):
    source = _seed(tmp_path / "beru.db")
    target = backup_database(database_url=f"sqlite+aiosqlite:///{source}")
    assert target.parent == source.parent
    assert target.name.startswith("beru.backup-")


@pytest.mark.asyncio
async def test_backup_round_trips_live_schema_and_rows(_engine, db_session):
    """The app's own async WAL schema + a row snapshot cleanly via the sync API."""
    conv = Conversation(title="backup me", agent="beru_core")
    db_session.add(conv)
    await db_session.commit()

    database = _engine.url.database
    assert database, "engine must be file-backed for this test"
    target = backup_database(database_url=f"sqlite+aiosqlite:///{database}")
    assert target.exists()
    counts = _compute_table_counts(target)
    assert counts["conversations"] == 1
    verify_db(target)


def test_verify_backup_detects_corrupted_target(tmp_path):
    source = _seed(tmp_path / "beru.db")
    target = backup_database(database_url=f"sqlite+aiosqlite:///{source}")
    # Corrupt the snapshot by truncating it after the header.
    with open(target, "r+b") as fh:
        fh.seek(len(b"SQLite format 3\x00"))
        fh.truncate()
    with pytest.raises(RuntimeError):
        verify_backup(source, target)


def test_verify_db_rejects_garbage(tmp_path):
    bogus = tmp_path / "not-a-db.db"
    bogus.write_bytes(b"this is definitely not a sqlite database file")
    with pytest.raises(RuntimeError):
        verify_db(bogus)


def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(database_url=f"sqlite+aiosqlite:///{tmp_path/'ghost.db'}")


# ---- restore ----


def test_restore_database_replaces_live_and_keeps_pre_copy(tmp_path):
    source = _seed(tmp_path / "beru.db", rows=2)
    snapshot = backup_database(database_url=f"sqlite+aiosqlite:///{source}")

    # Simulate drift: the live database gains rows after the snapshot.
    con = sqlite3.connect(str(source))
    try:
        con.execute("INSERT INTO conversations VALUES ('extra', 'x')")
        con.commit()
    finally:
        con.close()
    assert _compute_table_counts(source)["conversations"] == 3

    restored = restore_database(snapshot, database_url=f"sqlite+aiosqlite:///{source}")
    assert restored == source.resolve()
    # Row parity restored to the snapshot's counts.
    assert _compute_table_counts(restored)["conversations"] == 2
    # The drifted live file was preserved as a pre-restore copy.
    pre_restore = list(source.parent.glob("beru.pre-restore*.db"))
    assert len(pre_restore) == 1
    assert _compute_table_counts(pre_restore[0])["conversations"] == 3
    verify_db(restored)


def test_restore_rejects_corrupted_snapshot(tmp_path):
    bogus = tmp_path / "snapshot.db"
    bogus.write_bytes(b"not sqlite")
    with pytest.raises(RuntimeError):
        restore_database(bogus, database_url="sqlite:///./unused.db")


# ---- CLI ----


def test_cli_backup_and_verify(tmp_path, monkeypatch):
    source = _seed(tmp_path / "beru.db")
    target = tmp_path / "snap.db"
    url = f"sqlite+aiosqlite:///{source}"
    monkeypatch.setenv("DATABASE_URL", url)

    assert _main(["backup", "--target", str(target), "--no-verify"]) == 0
    assert target.exists()
    assert _main(["verify", str(target)]) == 0
    assert _main(["verify", str(source)]) == 0
    # Explicit --database overrides the env / settings.
    explicit = tmp_path / "explicit.db"
    assert _main(["backup", "--database", url, "--target", str(explicit)]) == 0
    assert explicit.exists()