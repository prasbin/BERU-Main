"""Database initialisation.

Schema is managed by **Alembic migrations** (see ``migrations/`` and
:mod:`backend.database.migrations`). On startup BERU brings the database up to
the latest revision. A database created by the older ``create_all`` path — with
the tables but no Alembic version bookkeeping — is adopted automatically
(stamped to head) rather than rebuilt.
"""

from __future__ import annotations

import asyncio

from backend.core.logging import get_logger
from backend.database.migrations import run_migrations

logger = get_logger(__name__)


async def init_db() -> None:
    """Bring the database schema up to the latest Alembic revision.

    Alembic runs synchronously (against a sync driver), so we execute it in a
    worker thread to avoid blocking — or nesting — the async event loop that is
    starting the application.
    """
    try:
        await asyncio.to_thread(run_migrations)
    except Exception as exc:  # noqa: BLE001 - surface a helpful startup error
        from backend.core.config import get_settings

        logger.exception(
            "Database migration failed. Check the database file and consult "
            "docs/architecture.md; BERU will not start on a mismatched schema."
        )
        raise RuntimeError(
            f"Database migration failed (see logs for details). "
            f"If a manual fix is needed, inspect {get_settings().database_url!r}."
        ) from exc
    logger.info("Database schema is up to date (Alembic migrations applied).")
