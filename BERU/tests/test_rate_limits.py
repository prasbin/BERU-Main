"""Tests for rate limiting and request size limits."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.agents.registry import get_agent_registry
from backend.api.rate_limit import RateLimiter
from backend.core.config import get_settings
from backend.database.base import Base, get_session
from backend.engines.intelligence import get_intelligence_engine
from backend.engines.llm.registry import reset_llm_provider
from backend.main import create_app
from backend.services.chat_service import reset_stream_session_factory, set_stream_session_factory
from backend.tools.registry import get_tool_registry


@pytest_asyncio.fixture
async def _sessionmaker(tmp_path):
    """Create a temp SQLite database with the full schema; yield a sessionmaker."""
    import backend.models  # noqa: F401

    db_path = tmp_path / "test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def client(_sessionmaker, monkeypatch):
    """Client with default settings (rate limit = 30 rpm)."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("BERU_API_KEY", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()

    async def override_get_session():
        async with _sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = override_get_session
    set_stream_session_factory(lambda: _sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()
    reset_stream_session_factory()
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()


# ---- Rate limiter unit tests ----


def test_rate_limiter_allows_within_limit():
    limiter = RateLimiter(rpm=5, burst=5, window_sec=60.0)
    for _ in range(5):
        limiter.check("1.2.3.4")


def test_rate_limiter_rejects_over_limit():
    limiter = RateLimiter(rpm=3, burst=3, window_sec=60.0)
    for _ in range(3):
        limiter.check("1.2.3.4")

    from backend.core.errors import RateLimitError

    with pytest.raises(RateLimitError):
        limiter.check("1.2.3.4")


def test_rate_limiter_unlimited_when_zero():
    limiter = RateLimiter(rpm=0, burst=0, window_sec=60.0)
    for _ in range(100):
        limiter.check("1.2.3.4")


def test_rate_limiter_separate_clients():
    limiter = RateLimiter(rpm=2, burst=2, window_sec=60.0)
    limiter.check("1.1.1.1")
    limiter.check("1.1.1.1")

    from backend.core.errors import RateLimitError

    with pytest.raises(RateLimitError):
        limiter.check("1.1.1.1")

    limiter.check("2.2.2.2")


def test_rate_limiter_cleanup():
    limiter = RateLimiter(rpm=10, burst=10, window_sec=0.05)
    limiter.check("1.2.3.4")
    assert "1.2.3.4" in limiter._clients

    import time

    time.sleep(0.15)
    limiter._cleanup()
    assert "1.2.3.4" not in limiter._clients


# ---- HTTP-level rate limit tests ----


@pytest.mark.asyncio
async def test_chat_rate_limit_enforced(client, monkeypatch):
    """Rate limit is enforced on the chat endpoint."""
    monkeypatch.setenv("BERU_RATE_LIMIT_CHAT_RPM", "3")
    get_settings.cache_clear()

    for _ in range(3):
        resp = await client.post("/api/v1/chat", json={"message": "hi"})
        assert resp.status_code == 200

    resp = await client.post("/api/v1/chat", json={"message": "blocked"})
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["type"] == "rate_limit_exceeded"
    assert "retry_after_seconds" in body["error"]["detail"]

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_stream_endpoint_rate_limited(client, monkeypatch):
    """Rate limit applies to the stream endpoint too."""
    monkeypatch.setenv("BERU_RATE_LIMIT_CHAT_RPM", "2")
    get_settings.cache_clear()

    for _ in range(2):
        resp = await client.post("/api/v1/chat/stream", json={"message": "hi"})
        assert resp.status_code == 200

    resp = await client.post("/api/v1/chat/stream", json={"message": "blocked"})
    assert resp.status_code == 429

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_rate_limit_disabled_when_zero(client, monkeypatch):
    """Setting RPM to 0 disables rate limiting."""
    monkeypatch.setenv("BERU_RATE_LIMIT_CHAT_RPM", "0")
    get_settings.cache_clear()

    for _ in range(20):
        resp = await client.post("/api/v1/chat", json={"message": "hi"})
        assert resp.status_code == 200

    get_settings.cache_clear()


# ---- Request size limit tests ----


@pytest.mark.asyncio
async def test_request_body_too_large(_sessionmaker, monkeypatch):
    """Requests exceeding max body size are rejected via Content-Length header."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("BERU_API_KEY", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("BERU_MAX_REQUEST_BODY_BYTES", "50")
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()

    async def override_get_session():
        async with _sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        import json as _json

        body_bytes = _json.dumps({"message": "x" * 200}).encode()
        resp = await c.post(
            "/api/v1/chat",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["type"] == "bad_request"
        assert "too large" in body["error"]["message"].lower()

    app.dependency_overrides.clear()
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()


@pytest.mark.asyncio
async def test_message_too_long_validated(client, monkeypatch):
    """Messages exceeding max length are rejected by Pydantic validation."""
    resp = await client.post(
        "/api/v1/chat",
        json={"message": "x" * 32_001},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["type"] == "validation_error"


@pytest.mark.asyncio
async def test_normal_request_accepted(client):
    """Normal-sized requests are accepted."""
    resp = await client.post("/api/v1/chat", json={"message": "Hello BERU"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health不受_rate_limit影响(client, monkeypatch):
    """Health endpoint is not subject to rate limiting."""
    monkeypatch.setenv("BERU_RATE_LIMIT_CHAT_RPM", "1")
    get_settings.cache_clear()

    for _ in range(10):
        resp = await client.get("/health")
        assert resp.status_code == 200

    get_settings.cache_clear()
