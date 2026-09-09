"""SQLite backup & restore for BERU.

Everything BERU persists lives in a single SQLite database file (conversations,
messages, facts, projects, proactive scheduled tasks / monitor triggers and their
run/fire history, notifications, and the reliability ledger's activity/audit
tables). A snapshot of that one file is therefore a complete backup.

Backups use the SQLite **online backup API** (``sqlite3.Connection.backup``),
which is the correct tool here:

- it is safe to run while the application is still up (WAL mode is enforced for
  every connection, so readers and writers coexist and ``backup`` captures the
  same consistent point-in-time view);
- it does **not** require the source to be quiescent or lock-free the way
  ``VACUUM INTO`` does, and it never rewrites the source file;
- the destination is written atomically as a normal SQLite file (no journal
  leftovers).

Every backup is verified on write:

- ``PRAGMA integrity_check`` must report ``ok`` on both the live file and the
  snapshot, and
- every non-internal table must contain the same number of rows in both.

Restoring is a file copy of the snapshot back over the live database path,
followed by an integrity check. **Stop the application first** — SQLite does not
support replacing the file under a running WAL-mode process safely.

Only file-backed SQLite databases are supported; passing a Postgres/MySQL URL
(or ``:memory:``) raises :class:`ValueError` with a clear message.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.database.migrations import to_sync_url

logger = get_logger(__name__)

# Used to enumerate user tables for the row-count comparison; the ``sqlite_*``
# internal tables are exempted (they hold schema bookkeeping and AUTOINCREMENT
# counters that legitimately differ between a live file and a snapshot).
_INTERNAL_TABLE_EXEMPT = {
    "sqlite_sequence",
    "sqlite_stat1",
    "sqlite_stat2",
    "sqlite_stat3",
    "sqlite_stat4",
}


def resolve_sqlite_path(database_url: str) -> Path:
    """Resolve the physical file backing a SQLAlchemy SQLite URL.

    Accepts both async (``sqlite+aiosqlite:///...``) and sync
    (``sqlite:///...``) forms, relative and absolute paths, and ignores any
    ``?query=...`` suffix. Raises :class:`ValueError` for non-SQLite or
    non-file (``:memory:``) URLs.
    """
    remainder = database_url.split("://", 1)
    if len(remainder) != 2:
        raise ValueError(f"Not a SQLAlchemy database URL: {database_url!r}")
    prefix, rest = remainder
    if "sqlite" not in prefix:
        raise ValueError(
            "backup/restore supports only file-backed SQLite databases; "
            f"unsupported URL prefix {prefix!r}"
        )
    if not rest.startswith("/"):
        raise ValueError(
            "backup/restore supports only file-backed SQLite databases "
            f"(got {database_url!r})"
        )
    if rest == "/:memory:" or ":memory:" in rest:
        raise ValueError("In-memory SQLite databases cannot be backed up")
    # rest == "/./beru.db"  -> path "./beru.db"
    # rest == "//abs/x.db"  -> path "/abs/x.db"
    absolute = rest.startswith("//")
    path = rest[1:] if absolute else rest.lstrip("/")
    # Drop any query-string (e.g. ?cache=shared or ?check_same_thread=1).
    path = path.split("?", 1)[0]
    return Path(path)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _compute_table_counts(path: Path) -> dict[str, int]:
    """Map of user table name -> row count for the database at ``path``."""
    con = _connect_readonly(path)
    try:
        names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        names -= _INTERNAL_TABLE_EXEMPT
        counts: dict[str, int] = {}
        for name in sorted(names):
            counts[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return counts
    finally:
        con.close()


def verify_db(path: Path) -> Path:
    """Run ``PRAGMA integrity_check`` against the database at ``path``.

    Returns the path when healthy; raises :class:`RuntimeError` with the
    integrity-check detail otherwise.
    """
    try:
        con = _connect_readonly(path)
        try:
            result = con.execute("PRAGMA integrity_check").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Database failed integrity check {path}: {exc}") from exc
    if result != [("ok",)]:
        detail = "; ".join(str(row[0]) for row in result)
        raise RuntimeError(f"Database failed integrity check {path}: {detail}")
    return path


def verify_backup(source: Path, target: Path) -> Path:
    """Confirm a snapshot is a faithful copy of its source.

    Checks integrity of both files and that every user table holds the same row
    count in each. Returns the target path, or raises :class:`RuntimeError`.
    """
    verify_db(source)
    verify_db(target)
    src_counts = _compute_table_counts(source)
    tgt_counts = _compute_table_counts(target)
    if src_counts != tgt_counts:
        differing = {
            t: (src_counts.get(t), tgt_counts.get(t))
            for t in set(src_counts) | set(tgt_counts)
            if src_counts.get(t) != tgt_counts.get(t)
        }
        raise RuntimeError(
            f"Snapshot {target} does not match source {source}; "
            f"row-count differences (table: source vs target): {differing}"
        )
    logger.info(
        "Backup verified: %s → %s (%d tables, %d rows)",
        source,
        target,
        len(src_counts),
        sum(src_counts.values()),
    )
    return target


def _default_target(source: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return source.with_name(f"{source.stem}.backup-{stamp}.db")


def backup_database(
    database_url: str | None = None,
    target: str | Path | None = None,
    verify: bool = True,
) -> Path:
    """Snapshot the live SQLite database to ``target`` (a timestamped sibling by
    default) using the SQLite online backup API.

    Keyword args:
        database_url: Override ``settings.database_url`` (tests pass a temp URL).
        target: Destination file path. Defaults to ``<db>.backup-<UTC stamp>.db``
            next to the source.
        verify: Integrity-check + row-count-compare the snapshot (default True).

    Returns the target :class:`Path`.
    """
    url = database_url or get_settings().database_url
    source = resolve_sqlite_path(to_sync_url(url))
    dest = Path(target) if target is not None else _default_target(source)
    dest = dest.resolve()
    if dest.parent != source.resolve().parent:
        dest.parent.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(
            f"Source database not found: {source}. Pass --target to an "
            "explicit destination if this is expected."
        )

    src = sqlite3.connect(f"{source}")
    try:
        dst = sqlite3.connect(f"{dest}")
        try:
            src.backup(dst)  # online backup; consistent across WAL frames
        finally:
            dst.close()
    finally:
        src.close()

    logger.info("Backup written: %s → %s", source, dest)
    if verify:
        verify_backup(source, dest)
    return dest


def restore_database(
    snapshot_path: str | Path,
    database_url: str | None = None,
    verify: bool = True,
) -> Path:
    """Replace the live database with a previously taken snapshot.

    The application must not be running against ``database_url`` while the file
    is replaced (SQLite does not support hot-swapping a WAL-mode database file).
    The snapshot is integrity-checked before it replaces the live file.

    Keyword args:
        snapshot_path: Path to a verified BERU snapshot file.
        database_url: Override ``settings.database_url``.
        verify: Integrity-check the snapshot and the restored file (default True).

    Returns the restored database :class:`Path`.
    """
    snapshot = Path(snapshot_path).resolve()
    verify_db(snapshot)
    url = database_url or get_settings().database_url
    live = resolve_sqlite_path(to_sync_url(url))

    import shutil

    backup_live = live.with_name(f"{live.stem}.pre-restore.db")
    if backup_live.exists():  # never clobber a previous pre-restore copy
        backup_live = live.with_name(
            f"{live.stem}.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
        )
    if live.exists():
        shutil.copy2(live, backup_live)
        logger.info("Pre-restore copy of live database saved: %s", backup_live)

    shutil.copy2(snapshot, live)
    if verify:
        verify_db(live)
    logger.info("Restored database %s from snapshot %s", live, snapshot)
    return live


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.services.backup",
        description="Back up (or verify) the BERU SQLite database.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bk = sub.add_parser("backup", help="Take a point-in-time snapshot.")
    bk.add_argument("--target", help="Destination snapshot file (default: timestamped sibling).")
    bk.add_argument(
        "--database",
        help="SQLAlchemy database URL to back up (default: settings DATABASE_URL).",
    )
    bk.add_argument("--no-verify", action="store_true", help="Skip integrity/row verification.")

    vr = sub.add_parser("verify", help="Verify an existing database or snapshot.")
    vr.add_argument("path", help="Database file to integrity-check.")

    args = parser.parse_args(argv)

    if args.command == "backup":
        target = backup_database(
            database_url=args.database,
            target=args.target,
            verify=not args.no_verify,
        )
        print(f"Backup written: {target}")
        return 0
    path = verify_db(Path(args.path))
    print(f"OK: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())