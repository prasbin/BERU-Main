"""Shared test-database helpers.

By default every test runs against a fresh, throwaway SQLite file in a temp
directory (hermetic — no services, no shared state). When the suite is pointed
at a PostgreSQL instance by setting ``BERU_TEST_DATABASE_URL`` (see
``scripts/test_postgres.py``), the shared fixtures instead bind to that
database and **drop/recreate the full schema before every test**, so each test
still starts from an empty database.

Only the fixtures that produce the app-facing database honour the env var;
tests that deliberately exercise SQLite-specific behaviour (file locking,
``PRAGMA`` hardening, online-backup semantics, in-memory URLs, …) build their
own SQLite files regardless, so they keep passing — and keep asserting real
SQLite behaviour — inside a Postgres run.
"""

from __future__ import annotations

import os
from pathlib import Path

TEST_DATABASE_URL: str = os.environ.get("BERU_TEST_DATABASE_URL", "").strip()

#: True when the suite is running against PostgreSQL (skips are keyed off this).
RUNNING_ON_POSTGRES: bool = bool(TEST_DATABASE_URL)


def is_postgres() -> bool:
    """True when the suite was launched against a Postgres test database."""
    return RUNNING_ON_POSTGRES


def test_database_url(tmp_path: object, name: str = "test.db") -> str:
    """Return the async SQLAlchemy URL for a test database.

    SQLite: a fresh ``<tmp_path>/<name>`` file. Postgres: the configured
    ``BERU_TEST_DATABASE_URL`` (shared — the schema is rebuilt per test by
    :func:`prepare_schema`).
    """
    if RUNNING_ON_POSTGRES:
        return TEST_DATABASE_URL
    return f"sqlite+aiosqlite:///{Path(str(tmp_path)) / name}"


def make_test_engine(tmp_path: object, name: str = "test.db"):
    """Create an async engine bound to the test database.

    Mirrors how the app engine is configured for each backend (SQLite gets
    ``check_same_thread=False``; Postgres gets no special connect args).
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    url = test_database_url(tmp_path, name=name)
    if url.startswith("sqlite"):
        return create_async_engine(url, connect_args={"check_same_thread": False})
    return create_async_engine(url, pool_pre_ping=True)


async def prepare_schema(engine) -> None:
    """Create the full schema on ``engine``.

    On SQLite (fresh temp file) this is ``create_all``. On Postgres the schema
    is dropped and recreated so the shared test database starts empty for each
    test, mirroring the per-test isolation of the SQLite path.

    Foreign-key constraints are dropped after ``create_all`` on Postgres so the
    suite keeps the exact semantics of the SQLite harness (whose test engines
    do not attach the production FK pragma): the many legacy tests that seed
    descendant rows directly would otherwise fail identically on both backends
    instead of varying between them. Production engines are unaffected — they
    enforce FKs on SQLite (pragma) and natively on Postgres.
    """
    import sqlalchemy as sa

    import backend.models  # noqa: F401  (register models on Base.metadata)
    from backend.database.base import Base

    async with engine.begin() as conn:
        if RUNNING_ON_POSTGRES:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            fk_rows = (
                await conn.execute(
                    sa.text(
                        "SELECT tc.table_name, tc.constraint_name "
                        "FROM information_schema.table_constraints tc "
                        "WHERE tc.constraint_type = 'FOREIGN KEY' "
                        "AND tc.table_schema = 'public'"
                    )
                )
            ).all()
            for table, name in fk_rows:
                await conn.execute(
                    sa.text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{name}"')
                )
        else:
            await conn.run_sync(Base.metadata.create_all)