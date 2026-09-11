"""PostgreSQL-only behaviour tests (Stage 5.2).

These run only when the suite is pointed at a PostgreSQL database via
``BERU_TEST_DATABASE_URL`` (see ``scripts/test_postgres.py``); on SQLite runs
the whole module is skipped. They exercise the pieces of Stage 5.2 that have no
SQLite equivalent or behave differently there: async/sync URL handling,
advisory locking, partial unique-index DDL and enforcement, upsert-on-conflict,
``pg_dump``/``pg_restore`` round-trips, and a full migration cycle.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import tests.db as testdb

pytestmark = pytest.mark.skipif(
    not testdb.RUNNING_ON_POSTGRES,
    reason="PostgreSQL-only: run via `python -m scripts.test_postgres`",
)

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from backend.database.base import _normalise_async_url  # noqa: E402
from backend.database.lock import (  # noqa: E402
    DatabaseLockError,
    acquire_db_lock,
)
from backend.database.migrations import (  # noqa: E402
    make_alembic_config,
    to_plain_uri,
    to_sync_url,
)
from backend.memory.vector_store import EmbeddingRow, VectorStore  # noqa: E402

PLAIN_URL = to_plain_uri(testdb.TEST_DATABASE_URL)
SYNC_URL = to_sync_url(testdb.TEST_DATABASE_URL)

import psycopg2  # noqa: E402


def _pg_sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def _pg_engine(tmp_path):
    """A fresh full schema on the shared Postgres test database."""
    engine = testdb.make_test_engine(tmp_path)
    await testdb.prepare_schema(engine)
    yield engine
    await engine.dispose()


def _drop_public_schema(plain_url: str) -> None:
    """Wipe the public schema (tables, indexes and alembic_version)."""
    with psycopg2.connect(plain_url) as conn:
        conn.set_session(autocommit=True)
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")


# ---- URL handling & engine connectivity ------------------------------------


def test_async_url_normalisation():
    assert (
        _normalise_async_url("postgresql://u:p@h:5432/d")
        == "postgresql+asyncpg://u:p@h:5432/d"
    )
    assert _normalise_async_url("postgres://u@h/d") == "postgresql+asyncpg://u@h/d"
    assert (
        _normalise_async_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    )
    assert (
        _normalise_async_url("postgresql+asyncpg://u@h/d")
        == "postgresql+asyncpg://u@h/d"
    )
    # The suite's own test URL must already be in async-app form.
    assert _normalise_async_url(testdb.TEST_DATABASE_URL) == testdb.TEST_DATABASE_URL


def test_sync_url_conversions_match_plain_uri():
    base = testdb.TEST_DATABASE_URL
    assert to_plain_uri(base).startswith("postgresql://")
    assert to_sync_url(base) == to_plain_uri(base).replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )


def test_psycopg2_sync_connect_select_one():
    with psycopg2.connect(PLAIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1


@pytest.mark.asyncio
async def test_async_engine_connects_and_selects(tmp_path):
    engine = testdb.make_test_engine(tmp_path)
    await testdb.prepare_schema(engine)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


# ---- Advisory lock ---------------------------------------------------------


def test_advisory_lock_holds_exclusive_and_releases():
    url = testdb.TEST_DATABASE_URL
    first = acquire_db_lock(url)
    assert first is not None
    assert hasattr(first, "acquire") and hasattr(first, "release")

    # A second instance in the same database cannot take the lock.
    with pytest.raises(DatabaseLockError):
        acquire_db_lock(url)

    first.release()

    # The lock is free again once released.
    second = acquire_db_lock(url)
    assert second is not None
    with pytest.raises(DatabaseLockError):
        acquire_db_lock(url)
    second.release()

    # And once more, to prove acquire/release is repeatable.
    third = acquire_db_lock(url)
    third.release()


# ---- Backup / verify / restore round-trip ----------------------------------


@pytest.mark.asyncio
async def test_backup_verify_restore_round_trip(_pg_engine, tmp_path):
    from backend.models import Fact
    from backend.services.backup import (
        backup_database,
        restore_database,
        verify_postgres_backup,
    )

    maker = _pg_sessionmaker(_pg_engine)
    async with maker() as session:
        session.add(Fact(key="a", value="1"))
        session.add(Fact(key="b", value="2"))
        await session.commit()

    target = tmp_path / "beru.dump"
    backup_database(testdb.TEST_DATABASE_URL, target=target, verify=True)
    assert target.exists()

    verified = verify_postgres_backup(testdb.TEST_DATABASE_URL, target)
    assert verified == target.resolve()

    # Mutate the live database away from the snapshot...
    async with maker() as session:
        row = (
            await session.execute(select(Fact).where(Fact.key == "a"))
        ).scalar_one()
        await session.delete(row)
        await session.commit()
    async with maker() as session:
        assert (
            await session.execute(select(func.count()).select_from(Fact))
        ).scalar_one() == 1

    # ...then restore the snapshot over it.
    restore_database(target, testdb.TEST_DATABASE_URL, verify=True)
    async with maker() as session:
        keys = set((await session.execute(select(Fact.key))).scalars())
    assert keys == {"a", "b"}


# ---- Migrations ------------------------------------------------------------


def test_migration_cycle_upgrade_downgrade_upgrade():
    from alembic import command

    _drop_public_schema(PLAIN_URL)
    cfg = make_alembic_config(SYNC_URL)

    command.upgrade(cfg, "head")

    with psycopg2.connect(PLAIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'facts'"
            )
            assert "user_id" in {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT i.relname, ix.indexdef, x.indisunique "
                "FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_indexes ix ON ix.indexname = i.relname "
                "WHERE i.relname IN "
                "('ix_facts_key', 'uq_facts_system_key', 'uq_projects_system_name')"
            )
            indexes = {name: (indexdef, is_unique) for name, indexdef, is_unique in cur.fetchall()}
            # The lookup index is non-unique; the partial ones are unique and
            # restricted to system/owner rows (user_id IS NULL).
            assert indexes["ix_facts_key"][1] is False
            assert indexes["uq_facts_system_key"][1] is True
            assert indexes["uq_projects_system_name"][1] is True
            assert "user_id IS NULL" in indexes["uq_facts_system_key"][0]
            assert "user_id IS NULL" in indexes["uq_projects_system_name"][0]
            cur.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'facts'::regclass AND contype = 'u'"
            )
            assert "uq_facts_user_key" in {r[0] for r in cur.fetchall()}

    # Downgrade to the pre-5.1 revision: accounts tables go away and the
    # original global unique indexes come back.
    command.downgrade(cfg, "b5c6d7e8f9a1")

    with psycopg2.connect(PLAIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users')")
            assert cur.fetchone()[0] is None
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'facts'"
            )
            assert "user_id" not in {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT x.indisunique FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "WHERE i.relname = 'ix_facts_key'"
            )
            assert cur.fetchone()[0] is True

    # Upgrade again: the re-applied migration must reproduce the head schema.
    command.upgrade(cfg, "head")

    with psycopg2.connect(PLAIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users')")
            assert cur.fetchone()[0] is not None
            cur.execute(
                "SELECT i.relname FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "WHERE i.relname = 'uq_facts_system_key'"
            )
            assert cur.fetchall()

    # The stamped revision must be exactly the single head.
    from alembic.script import ScriptDirectory

    with psycopg2.connect(PLAIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            version = cur.fetchone()[0]
    heads = ScriptDirectory.from_config(make_alembic_config(SYNC_URL)).get_heads()
    assert heads == ["e2f3a4b5c6d7"]
    assert version == heads[0]


def test_run_migrations_adopts_create_all_schema():
    """Application startup against a schema built by create_all stamps head."""
    from backend.database.base import Base
    from backend.database.migrations import run_migrations

    _drop_public_schema(PLAIN_URL)
    engine = create_engine(SYNC_URL)
    Base.metadata.create_all(engine)
    engine.dispose()

    run_migrations(SYNC_URL)

    with psycopg2.connect(PLAIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            version = cur.fetchone()[0]
    assert version == "e2f3a4b5c6d7"


# ---- Partial unique index enforcement --------------------------------------


@pytest.mark.asyncio
async def test_system_row_uniqueness_enforced_by_partial_indexes(_pg_engine):
    from backend.models import Fact, Project

    maker = _pg_sessionmaker(_pg_engine)
    async with maker() as session:
        session.add(Fact(key="owner-key", value="1"))
        await session.commit()
        session.add(Fact(key="owner-key", value="2"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async with maker() as session:
        session.add(Project(name="Alpha"))
        await session.commit()
        session.add(Project(name="Alpha"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_per_user_and_system_uniqueness(_pg_engine):
    from backend.models import Fact, User

    maker = _pg_sessionmaker(_pg_engine)
    async with maker() as session:
        alice = User(username="alice")
        bob = User(username="bob")
        session.add_all([alice, bob])
        await session.commit()
        alice_id, bob_id = alice.id, bob.id

    async with maker() as session:
        session.add(Fact(key="theme", value="dark", user_id=alice_id))
        await session.commit()
        # A distinct user may remember the same key independently.
        session.add(Fact(key="theme", value="light", user_id=bob_id))
        await session.commit()
        # The same user cannot.
        session.add(Fact(key="theme", value="force", user_id=alice_id))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async with maker() as session:
        rows = (await session.execute(select(Fact))).scalars().all()
    assert {fact.user_id for fact in rows} == {alice_id, bob_id}


# ---- Vector-store upsert ---------------------------------------------------


@pytest.mark.asyncio
async def test_vector_store_upsert_overwrites_on_conflict(_pg_engine):
    store = VectorStore()
    maker = _pg_sessionmaker(_pg_engine)

    async with maker() as session:
        await store.upsert(
            session, source_table="facts", source_id="f1", text="first", vector=[1.0, 0.0]
        )
        await session.commit()
        # Same (source_table, source_id): must overwrite, not duplicate.
        await store.upsert(
            session, source_table="facts", source_id="f1", text="second", vector=[0.0, 1.0]
        )
        await session.commit()

    async with maker() as session:
        assert await store.count(session, source_table="facts") == 1
        row = (
            await session.execute(
                select(EmbeddingRow).where(EmbeddingRow.source_id == "f1")
            )
        ).scalar_one()
        assert row.text == "second"
        assert row.dimension == 2
        results = await store.search(session, [0.0, 1.0], source_table="facts")
        assert len(results) == 1
        assert results[0].text == "second"
        assert results[0].source_id == "f1"