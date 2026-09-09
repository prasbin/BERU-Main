"""Alembic migration plumbing.

This module is the single place that knows how to build an Alembic
configuration from BERU's application settings and apply migrations. Both the
CLI (``alembic upgrade head``) and application startup (:func:`init_db`) route
through the same ``migrations/`` directory and the same ``DATABASE_URL`` so
there is exactly one source of truth for the schema.

Design notes:

* Alembic runs against a **synchronous** driver even though the application
  uses an async engine. Migrations are short, run at startup or from the CLI,
  and a sync engine avoids nesting an event loop inside another (Alembic's
  async template calls ``asyncio.run`` internally). :func:`to_sync_url`
  converts the configured async URL to its sync equivalent.
* ``alembic`` is imported lazily inside functions so merely importing this
  module (which happens transitively via ``backend.main``) never requires
  Alembic to be installed. Only actually *running* migrations does.
* The database URL is never hardcoded — it is read from settings (or an
  explicit override, used by tests), honouring the project's secrets policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.config import get_settings
from backend.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from alembic.config import Config

logger = get_logger(__name__)

# .../BERU/backend/database/migrations.py -> parents[2] == project root (BERU/).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

# Map async SQLAlchemy driver prefixes to a synchronous equivalent for Alembic.
_ASYNC_TO_SYNC_DRIVER = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql+psycopg2",
    "mysql+aiomysql": "mysql+pymysql",
}


def to_sync_url(url: str) -> str:
    """Return the synchronous-driver form of an async SQLAlchemy URL.

    Alembic (and the transient engine we use to inspect the database) run
    synchronously. ``sqlite+aiosqlite:///./beru.db`` becomes
    ``sqlite:///./beru.db``. URLs that are already synchronous pass through
    unchanged.
    """
    for async_prefix, sync_prefix in _ASYNC_TO_SYNC_DRIVER.items():
        if url.startswith(async_prefix):
            return sync_prefix + url[len(async_prefix) :]
    return url


def make_alembic_config(url: str | None = None) -> Config:
    """Build an Alembic ``Config`` pointed at BERU's ``migrations/`` directory.

    The ``sqlalchemy.url`` is set to an explicit ``url`` when given (tests pass
    a throwaway database), otherwise to the application's ``DATABASE_URL``
    converted to a sync driver. Because it is set here, ``env.py`` uses it for
    both online and offline runs without any ``.env`` lookup of its own.
    """
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    resolved = url or to_sync_url(get_settings().database_url)
    cfg.set_main_option("sqlalchemy.url", resolved)
    return cfg


def run_migrations(url: str | None = None) -> None:
    """Bring the database up to the latest revision (``head``).

    A database created by the pre-migration ``create_all`` path has the tables
    but no ``alembic_version`` bookkeeping. Rather than fail trying to
    re-create existing tables, such a database is *adopted* by stamping it to
    ``head``. A fresh database (no tables, no version) is upgraded normally.
    """
    import sqlalchemy as sa
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    cfg = make_alembic_config(url)
    sync_url = cfg.get_main_option("sqlalchemy.url")

    # Inspect current state with a short-lived sync connection.
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            current_revision = MigrationContext.configure(conn).get_current_revision()
            has_legacy_tables = sa.inspect(conn).has_table("conversations")
    finally:
        engine.dispose()

    if current_revision is None and has_legacy_tables:
        _adopt_legacy_database(cfg, sync_url)
    else:
        command.upgrade(cfg, "head")


def _adopt_legacy_database(cfg: Config, sync_url: str) -> None:
    """Stamp an un-versioned legacy database to ``head`` — only when safe.

    The pre-migration ``create_all`` path left tables with no Alembic
    bookkeeping. Stamping blindly to ``head`` would silently skip every schema
    change the migrations introduced (new tables, columns, constraints), so we
    first verify the existing schema covers every table the current models
    declare and refuse to adopt otherwise with a clear, actionable error.
    """
    import sqlalchemy as sa
    from alembic import command

    import backend.models  # noqa: F401  (register models on Base.metadata)
    from backend.database.base import Base

    expected = set(Base.metadata.tables)
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            existing = set(sa.inspect(conn).get_table_names())
    finally:
        engine.dispose()

    missing = sorted(table for table in expected if table not in existing)
    if missing:
        raise RuntimeError(
            "Refusing to auto-adopt the existing un-versioned database: the "
            "schema is missing tables the current BERU revision requires "
            f"({', '.join(missing)}). It predates one or more migration "
            "features, so stamping to head would corrupt it. Back up the "
            "database file and either start fresh or migrate its data manually."
        )

    logger.info(
        "Existing un-versioned database detected; stamping to head "
        "(adopting the schema created by the legacy create_all path)."
    )
    command.stamp(cfg, "head")
