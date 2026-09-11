"""Run the BERU test suite against a PostgreSQL database.

By default this provisions an **embedded** PostgreSQL cluster with the optional
``pgserver`` package (kept out of ``requirements.txt`` on purpose), creates a
throwaway ``beru_test`` database, and points the suite at it via
``BERU_TEST_DATABASE_URL``. The cluster is stopped and its data directory
deleted when the run finishes.

Usage::

    pip install pgserver            # optional, embedded runs only
    python -m scripts.test_postgres
    python -m scripts.test_postgres -- --tb=short -k "multiuser or postgres"

To run against an already-running PostgreSQL instead (e.g. a CI service
container or a local server), pass its URL::

    python -m scripts.test_postgres --url "postgresql://postgres:secret@localhost/beru_test"

The URL may be a plain ``postgresql://`` DSN; it is upgraded to the async
``+asyncpg`` form automatically (see ``backend.database.base``). Synchronous
tooling (Alembic, ``pg_dump``/``pg_restore``) receives the plain URI.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PGDATA = REPO_ROOT / ".pgtest" / "pgdata"


def _print_db(uri: str) -> None:
    print(f"PostgreSQL test database: {uri}")


def _normalise_async_url(url: str) -> str:
    """Mirror the app's URL normalisation so tests get an asyncpg DSN."""
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _run_pytest(async_url: str, pytest_args: list[str]) -> int:
    env = dict(os.environ)
    env["BERU_TEST_DATABASE_URL"] = async_url
    cmd = [sys.executable, "-m", "pytest", *pytest_args]
    print("Running:", " ".join(cmd))
    return subprocess.call(cmd, env=env, cwd=REPO_ROOT)


def _start_embedded(pgdata: Path, keep: bool) -> str:
    """Provision an embedded cluster and return its async test database URL."""
    try:
        from pgserver import get_server
    except ImportError as exc:  # pragma: no cover - user-facing help
        raise SystemExit(
            "Embedded PostgreSQL needs the optional 'pgserver' package. "
            "Install it (pip install pgserver) or point --url at an existing "
            "PostgreSQL instance."
        ) from exc

    pgdata.parent.mkdir(parents=True, exist_ok=True)
    server = get_server(pgdata, cleanup_mode="stop" if keep else "delete")
    try:
        probe = server.psql(
            "SELECT 1 FROM pg_database WHERE datname = 'beru_test';"
        )
    except subprocess.CalledProcessError:  # pragma: no cover - defensive
        probe = ""
    if "1" not in probe:
        server.psql('CREATE DATABASE "beru_test";')

    base_uri = server.get_uri(database="beru_test")
    async_url = _normalise_async_url(base_uri)
    print(f"Embedded PostgreSQL started (pgdata: {pgdata})")
    return async_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the BERU test suite against PostgreSQL "
            "(embedded pgserver by default)."
        )
    )
    parser.add_argument(
        "--url",
        help="Use an existing PostgreSQL DSN instead of the embedded server.",
    )
    parser.add_argument(
        "--pgdata",
        default=str(DEFAULT_PGDATA),
        help="Embedded cluster data directory (default: .pgtest/pgdata).",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the embedded cluster's data directory after the run.",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="Extra arguments forwarded to pytest (e.g. -- --tb=short).",
    )
    args = parser.parse_args(argv)

    if args.url:
        async_url = _normalise_async_url(args.url)
        _print_db(async_url)
        return _run_pytest(async_url, args.pytest_args)

    async_url = _start_embedded(Path(args.pgdata), keep=args.keep)
    _print_db(async_url)
    code = _run_pytest(async_url, args.pytest_args)
    if args.keep:
        print(f"Kept embedded cluster data at {args.pgdata}")
    else:
        print("Embedded PostgreSQL cluster stopped and cleaned up.")
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())