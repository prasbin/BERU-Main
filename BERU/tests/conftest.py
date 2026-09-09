"""Shared pytest fixtures.

Every test runs against a fresh, file-backed SQLite database in a temp directory
and forces the deterministic mock LLM provider, so the suite is fully hermetic
(no network, no API keys, no shared state between tests).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.agents.registry import get_agent_registry
from backend.core.config import get_settings
from backend.database.base import Base, get_engine, get_session
from backend.engines.intelligence import get_intelligence_engine
from backend.engines.llm.registry import reset_llm_provider
from backend.main import create_app
from backend.tools.registry import get_tool_registry


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    """Force the mock provider and reset cached singletons before each test."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    # Keep background scheduler/monitor loops out of tests by default; the
    # proactive tests drive them explicitly via the shared runtime.
    monkeypatch.setenv("BERU_PROACTIVE_ENABLED", "false")
    # Clear caches so settings/provider/engine are rebuilt from the patched env.
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()
    yield
    from backend.services.chat_service import reset_stream_session_factory
    from backend.services.proactive_service import reset_proactive_runtime

    reset_proactive_runtime()
    reset_stream_session_factory()
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    from backend.services.activity_ledger import reset_activity_ledger

    reset_activity_ledger()
    from backend.api.security import reset_session_store

    reset_session_store()
    from backend.services.observability import reset_request_metrics

    reset_request_metrics()


@pytest_asyncio.fixture
async def _engine(tmp_path):
    """A temp SQLite database with the full schema; yield its async engine."""
    import backend.models  # noqa: F401  (register models on Base.metadata)

    db_path = tmp_path / "test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def _sessionmaker(_engine):
    """An async sessionmaker bound to the temp database."""
    return async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db_session(_sessionmaker) -> AsyncSession:
    async with _sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def client(_sessionmaker, _engine) -> AsyncClient:
    """An HTTP client bound to the app, with the DB session dependency overridden."""

    # Background proactive hooks (execution state persistence, alert pushes) use
    # the shared runtime's session factory; point it at this test's temp DB so
    # no writes leak into the real ./beru.db during HTTP tests. Restored to the
    # default by the autouse teardown.
    from backend.services.chat_service import set_stream_session_factory
    from backend.services.proactive_service import set_session_factory

    set_session_factory(lambda: _sessionmaker)
    set_stream_session_factory(lambda: _sessionmaker)
    # Same indirection for the durable reliability ledger.
    from backend.services.activity_ledger import (
        get_activity_ledger,
        set_ledger_session_factory,
    )

    set_ledger_session_factory(lambda: _sessionmaker)

    async def override_get_session():
        async with _sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = override_get_session
    # Point the readiness probe's engine at the temp DB too, so /ready in tests
    # never touches the real ./beru.db.
    app.dependency_overrides[get_engine] = lambda: _engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    # Let best-effort ledger writes finish against the live temp DB.
    try:
        await get_activity_ledger().flush()
    except Exception:  # noqa: BLE001 - teardown must not mask test results
        pass
    app.dependency_overrides.clear()
