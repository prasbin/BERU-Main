"""Backup & restore for BERU (SQLite and PostgreSQL).

Everything BERU persists lives in the database ``DATABASE_URL`` points at
(conversations, messages, facts, projects, proactive scheduled tasks / monitor
triggers and their run/fire history, notifications, and the reliability
ledger's activity/audit tables). A snapshot of that store is therefore a
complete backup.

SQLite
------
Backups use the SQLite **online backup API** (``sqlite3.Connection.backup``),
which is the correct tool here:

- it is safe to run while the application is still up (WAL mode is enforced for
  every connection, so readers and writers coexist and ``backup`` captures the
  same consistent point-in-time view);
- it does **not** require the source to be quiescent or lock-free the way
  ``VACUUM INTO`` does, and it never rewrites the source file;
- the destination is written atomically as a normal SQLite file (no journal
  leftovers).

PostgreSQL
----------
Backups use ``pg_dump`` (custom format): a point-in-time logical snapshot that
contains the full schema and data. ``pg_restore`` replaces the live database
from such a snapshot. The tools are looked up on ``PATH`` first and then in the
embedded ``pgserver`` bundle (so the driver-agnostic harness works without a
system PostgreSQL install).

Every backup is verified on write:

- SQLite: ``PRAGMA integrity_check`` must report ``ok`` on both the live file
  and the snapshot, and every non-internal table must contain the same number
  of rows in both.
- PostgreSQL: the archive must parse (``pg_restore --list``) and — when the
  connection has permission — restoring it into a throwaway database must
  reproduce the same row counts as the live database.

Restoring replaces the live database (a file copy for SQLite, a
``pg_restore --clean`` reload for PostgreSQL). **Stop the application first** —
SQLite does not support replacing the file under a running WAL-mode process
safely, and Postgres restores a torn schema better with the app quiesced.

Only SQLite and PostgreSQL are supported; passing another scheme (or ``:memory:``)
raises :class:`ValueError` with a clear message.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.database.migrations import to_plain_uri, to_sync_url

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


def _scheme(url: str) -> str:
    """Return the base URL scheme (before ``://``), driver suffix stripped."""
    prefix = url.split("://", 1)[0].lower()
    base = prefix.split("+", 1)[0] if "+" in prefix else prefix
    return "postgres" if base.startswith("postgres") else base


def _default_pg_target(plain_url: str) -> Path:
    """Derive a sibling snapshot path named after the database name."""
    from urllib.parse import unquote

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    body = plain_url.split("://", 1)[1]
    db_name = unquote(body.split("/", 1)[1].split("?", 1)[0]) if "/" in body else "beru"
    name = db_name or "beru"
    cwd = Path.cwd()
    return (cwd / f"{name}.backup-{stamp}.dump").resolve()


def backup_database(
    database_url: str | None = None,
    target: str | Path | None = None,
    verify: bool = True,
) -> Path:
    """Snapshot the live database to ``target`` (a timestamped path by default).

    Keyword args:
        database_url: Override ``settings.database_url`` (tests pass a temp URL).
        target: Destination snapshot path. Defaults to a timestamped sibling
            of the source database.
        verify: Verify the snapshot (default True) — integrity plus row-count
            comparison (SQLite), or archive parsing plus a throwaway restore
            when the connection permits it (PostgreSQL).

    Returns the target :class:`Path`.
    """
    url = database_url or get_settings().database_url
    scheme = _scheme(url)
    if scheme == "sqlite":
        return _backup_sqlite(url, target, verify)
    if scheme == "postgres":
        return _backup_postgres(url, target, verify)
    raise ValueError(
        f"backup/restore supports only SQLite or PostgreSQL databases; "
        f"unsupported URL scheme {_scheme(url)!r}"
    )


def _backup_sqlite(
    database_url: str | None,
    target: str | Path | None,
    verify: bool,
) -> Path:
    """SQLite online-backup snapshot (see :func:`backup_database`)."""
    source = resolve_sqlite_path(to_sync_url(database_url))
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


def _pg_tool(name: str) -> str:
    """Locate a PostgreSQL helper binary (PATH first, then pgserver's bundle)."""
    on_path = shutil.which(name)
    if on_path:
        return on_path
    try:
        import pgserver
    except ImportError:
        pgserver_root: Path | None = None
    else:
        pgserver_root = Path(pgserver.__file__).resolve().parent / "pginstall" / "bin"
    if pgserver_root is not None and pgserver_root.is_dir():
        exe = pgserver_root / (name + ".exe" if os.name == "nt" else name)
        if exe.exists():
            return str(exe)
    raise RuntimeError(
        f"{name!r} was not found on PATH or in the embedded Postgres bundle. "
        "Install PostgreSQL (or run the suite through scripts/test_postgres.py "
        "which provisions an embedded instance)."
    )


def _pg_run(args: list[str], label: str) -> None:
    """Run a PostgreSQL helper, raising RuntimeError with output on failure."""
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{label} failed (exit {result.returncode}): {detail}")
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.warning("%s: %s", label, line)


def _backup_postgres(
    database_url: str | None,
    target: str | Path | None,
    verify: bool,
) -> Path:
    """PostgreSQL ``pg_dump`` snapshot (see :func:`backup_database`)."""
    plain = to_plain_uri(database_url)
    dest = Path(target) if target is not None else _default_pg_target(plain)
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # A fresh archive must never silently overwrite an earlier backup: reject a
    # pre-existing destination before invoking pg_dump.
    if dest.exists():
        raise FileExistsError(f"Backup target already exists: {dest}")

    dump = _pg_tool("pg_dump")
    _pg_run(
        [
            dump,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--file={dest}",
            plain,
        ],
        "pg_dump",
    )
    logger.info("Backup written: %s → %s", plain, dest)
    if verify:
        verify_postgres_backup(plain, dest)
    return dest


def _pg_table_counts(plain_url: str) -> dict[str, int]:
    """Map of public-schema base table name -> row count for a Postgres URL."""
    import psycopg2

    with psycopg2.connect(plain_url) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
            tables = [row[0] for row in cur.fetchall()]
            counts: dict[str, int] = {}
            for table in tables:
                cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                counts[table] = cur.fetchone()[0]
    return counts


def _as_admin_url(plain_url: str, database: str) -> str:
    """Point ``plain_url`` at the maintenance database ``database``."""
    scheme, separator, rest = plain_url.partition("://")
    if not separator:
        raise ValueError(f"Not a libpq URL: {plain_url!r}")
    host_part, _, _ = rest.partition("/")
    return f"{scheme}://{host_part}/{database}"


def _pg_temp_database_name() -> str:
    import random

    return f"beru_backup_verify_{os.getpid()}_{random.randrange(10**6):06d}"


def verify_postgres_backup(source_url: str, target: str | Path) -> Path:
    """Confirm a pg_dump archive faithfully captures its source.

    The archive must parse (``pg_restore --list``), and — when the connection
    can create databases — restoring it into a throwaway database must reproduce
    the same public-schema row counts as the source. Returns the target path,
    or raises :class:`RuntimeError`.
    """
    target = Path(target).resolve()
    plain = to_plain_uri(source_url)
    _pg_run([_pg_tool("pg_restore"), "--list", str(target)], "pg_restore --list")

    import psycopg2

    admin_url = _as_admin_url(plain, "postgres")

    def _try_create(temp_db: str) -> bool:
        try:
            conn = psycopg2.connect(admin_url)
        except psycopg2.OperationalError:
            return False
        try:
            # Autocommit must be enabled *before* any transaction begins:
            # CREATE DATABASE cannot run inside a transaction block.
            conn.set_session(autocommit=True)
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{temp_db}"')
        finally:
            conn.close()
        return True

    temp_db = _pg_temp_database_name()
    temp_url: str | None = None
    try:
        if not _try_create(temp_db):
            logger.warning(
                "Cannot create a throwaway database to verify the Postgres "
                "backup (no permission); verified the archive parses only."
            )
            return target
        temp_url = _as_admin_url(plain, temp_db)
        _pg_run(
            [
                _pg_tool("pg_restore"),
                "--no-owner",
                "--no-privileges",
                f"--dbname={temp_url}",
                str(target),
            ],
            "pg_restore",
        )
        src_counts = _pg_table_counts(plain)
        tgt_counts = _pg_table_counts(temp_url)
        if src_counts != tgt_counts:
            differing = {
                t: (src_counts.get(t), tgt_counts.get(t))
                for t in set(src_counts) | set(tgt_counts)
                if src_counts.get(t) != tgt_counts.get(t)
            }
            raise RuntimeError(
                f"Snapshot {target} does not match source {plain}; "
                f"row-count differences (table: source vs snapshot): {differing}"
            )
        logger.info(
            "Backup verified: %s → %s (%d tables, %d rows)",
            plain,
            target,
            len(src_counts),
            sum(src_counts.values()),
        )
    finally:
        # The throwaway database is dropped on every exit path, success or
        # failure (a failed comparison leaves it half-restored).
        if temp_url is not None:
            import psycopg2

            conn = psycopg2.connect(admin_url)
            try:
                conn.set_session(autocommit=True)
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{temp_db}"')
            finally:
                conn.close()
    return target


def restore_database(
    snapshot_path: str | Path,
    database_url: str | None = None,
    verify: bool = True,
) -> Path:
    """Replace the live database with a previously taken snapshot.

    The application must not be running against ``database_url`` while the
    snapshot replaces the live database.

    Keyword args:
        snapshot_path: Path to a verified BERU snapshot.
        database_url: Override ``settings.database_url``.
        verify: Verify the restored database (default True).

    Returns the restored database path (SQLite) or the snapshot path
    (PostgreSQL).
    """
    url = database_url or get_settings().database_url
    snapshot = Path(snapshot_path).resolve()
    if _scheme(url) == "sqlite":
        return _restore_sqlite(snapshot, url, verify)
    if _scheme(url) == "postgres":
        return _restore_postgres(snapshot, url, verify)
    raise ValueError(
        f"backup/restore supports only SQLite or PostgreSQL databases; "
        f"unsupported URL scheme {_scheme(url)!r}"
    )


def _restore_sqlite(snapshot: Path, database_url: str, verify: bool) -> Path:
    """SQLite file-copy restore (see :func:`restore_database`)."""
    verify_db(snapshot)
    live = resolve_sqlite_path(to_sync_url(database_url))

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


def _restore_postgres(snapshot: Path, database_url: str, verify: bool) -> Path:
    """PostgreSQL ``pg_restore --clean`` reload (see :func:`restore_database`)."""
    if not snapshot.exists():  # pragma: no cover - guarded by callers
        raise FileNotFoundError(f"Backup archive not found: {snapshot}")
    plain = to_plain_uri(database_url)
    _pg_run(
        [
            _pg_tool("pg_restore"),
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            f"--dbname={plain}",
            str(snapshot),
        ],
        "pg_restore",
    )
    if verify:
        # The archive was already verified on write; re-parse it here so a
        # corrupted/truncated copy is still caught before we call it restored.
        _pg_run([_pg_tool("pg_restore"), "--list", str(snapshot)], "pg_restore --list")
    logger.info("Restored database %s from snapshot %s", plain, snapshot)
    return snapshot


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.services.backup",
        description="Back up (or verify) the BERU database (SQLite or PostgreSQL).",
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