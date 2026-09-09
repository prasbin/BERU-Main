"""Alembic migration environment for BERU.

Runs migrations with a **synchronous** engine derived from the application's
``DATABASE_URL`` (converted from its async driver). Using a sync engine keeps
migrations identical whether invoked from the CLI or from application startup,
and avoids nesting event loops.

The target metadata is BERU's ORM ``Base.metadata`` (with all models imported
for their registration side effect), so ``alembic revision --autogenerate``
sees the full schema.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the models registers them on Base.metadata (needed for autogenerate).
import backend.models  # noqa: F401
from backend.core.config import get_settings
from backend.database.base import Base
from backend.database.migrations import to_sync_url

# Alembic Config object providing access to values in alembic.ini (if any).
config = context.config

# Configure Python logging from the ini file only when run via the CLI. When
# the config is built programmatically (application startup / tests) there is
# no ini file, so we skip this and inherit the app's logging setup.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the sync database URL.

    An explicitly configured URL (set by :func:`make_alembic_config` or an
    ``.ini``) wins; otherwise fall back to application settings, converted to a
    synchronous driver.
    """
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return to_sync_url(get_settings().database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (``--sql`` mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-safe ALTERs for future migrations.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-safe ALTERs for future migrations.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
