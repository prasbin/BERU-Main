"""SQLAlchemy 2.0 async foundation.

Engine and session factory are created lazily and cached, so importing this
module has no side effects (no file/connection is opened until first use). This
also lets tests swap the database by overriding the :func:`get_session`
dependency without touching module import order.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from backend.core.config import get_settings


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Apply per-connection SQLite safety pragmas on every new connection.

    - ``foreign_keys=ON``: without it SQLAlchemy's ``ON DELETE`` clauses are
      ignored and ``DELETE`` statements silently orphan child rows.
    - ``busy_timeout=5000``: fail fast instead of the default 5s hang when the
      database is briefly locked by a concurrent writer.
    - ``journal_mode=WAL``: persistent database setting enabling concurrent
      readers/writers; harmless to re-assert on reconnect.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for all ORM models.

    ``AsyncAttrs`` enables ``await obj.awaitable_attrs.<relationship>`` for lazy
    loading under async sessions when needed.
    """


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, created on first use."""
    settings = get_settings()
    connect_args: dict = {}
    url = settings.database_url
    db_prefix = "sqlite" if url.startswith("sqlite") else "postgres"
    if url.startswith("sqlite"):
        # Allow SQLite connections to be used across the async event loop tasks.
        connect_args["check_same_thread"] = False
        # Note: no explicit pool_size here — aiosqlite resolves NullPool by
        # default, so each short-lived session uses its own connection and
        # long LLM streams (which no longer hold sessions) never pin the pool.
    engine = create_async_engine(
        _normalise_async_url(url),
        echo=settings.db_echo,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    if db_prefix == "sqlite":
        event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    return engine


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory."""
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        class_=AsyncSession,
    )


def _normalise_async_url(url: str) -> str:
    """Return ``url`` in a form the async engine can consume.

    A bare ``postgresql://`` (or legacy ``postgres://``) DSN has no async
    driver, so it is upgraded to asyncpg; ``sqlite`` / already-qualified URLs
    pass through unchanged.
    """
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a scoped async session.

    The session is closed automatically when the request finishes. Callers are
    responsible for committing writes; on an unhandled exception the context
    manager rolls back.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
