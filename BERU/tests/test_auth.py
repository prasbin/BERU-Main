"""Tests for single-user API key authentication."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.agents.registry import get_agent_registry
from backend.core.config import Settings, get_settings
from backend.database.base import Base, get_session
from backend.engines.intelligence import get_intelligence_engine
from backend.engines.llm.registry import reset_llm_provider
from backend.main import create_app
from backend.tools.registry import get_tool_registry

TEST_API_KEY = "test-secret-key-12345"


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
async def auth_client(_sessionmaker, monkeypatch):
    """Client with auth enabled."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("BERU_API_KEY", TEST_API_KEY)
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()
    from backend.api.security import reset_session_store

    reset_session_store()


@pytest.mark.asyncio
async def test_auth_disabled_allows_access_without_key(monkeypatch, _sessionmaker):
    """When no API key is configured, routes work without the header."""
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/api/v1/status")
        assert resp.status_code == 200
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()


@pytest.mark.asyncio
async def test_auth_enabled_rejects_missing_key(auth_client):
    """With auth enabled, missing X-API-Key returns 401."""
    resp = await auth_client.get("/api/v1/status")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_auth_enabled_rejects_wrong_key(auth_client):
    """With auth enabled, wrong X-API-Key returns 401."""
    resp = await auth_client.get("/api/v1/status", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_auth_enabled_accepts_correct_key(auth_client):
    """With auth enabled, correct X-API-Key allows access."""
    resp = await auth_client.get("/api/v1/status", headers={"X-API-Key": TEST_API_KEY})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint_not_protected(auth_client):
    """Health endpoint works without API key even when auth is enabled."""
    resp = await auth_client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_root_endpoint_not_protected(auth_client):
    """Root endpoint works without API key even when auth is enabled."""
    resp = await auth_client.get("/")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_chat_requires_api_key(auth_client):
    """Chat endpoint requires API key when auth is enabled."""
    resp = await auth_client.post(
        "/api/v1/chat",
        json={"message": "Hello"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_chat_accepts_correct_key(auth_client):
    """Chat endpoint works with correct API key."""
    resp = await auth_client.post(
        "/api/v1/chat",
        json={"message": "Hello"},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_conversations_requires_api_key(auth_client):
    """Conversations endpoint requires API key when auth is enabled."""
    resp = await auth_client.get("/api/v1/conversations")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_agents_requires_api_key(auth_client):
    """Agents endpoint requires API key when auth is enabled."""
    resp = await auth_client.get("/api/v1/agents")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_tools_requires_api_key(auth_client):
    """Tools endpoint requires API key when auth is enabled."""
    resp = await auth_client.get("/api/v1/tools")
    assert resp.status_code == 401


def test_localhost_check_refuses_non_localhost_without_key(monkeypatch):
    """App refuses to start on non-localhost without auth configured."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("BERU_API_KEY", raising=False)
    monkeypatch.setenv("HOST", "0.0.0.0")
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()

    with pytest.raises(SystemExit, match="Refusing to start"):
        create_app()

    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()


def test_localhost_check_allows_non_localhost_with_key(monkeypatch):
    """App starts on non-localhost when auth is configured."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("BERU_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("HOST", "0.0.0.0")
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()

    app = create_app()
    assert app is not None

    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()


def test_localhost_check_allows_localhost_without_key(monkeypatch):
    """App starts on localhost without auth configured."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("BERU_API_KEY", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()

    app = create_app()
    assert app is not None

    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()


# ---- Session-cookie flow ----


@pytest.mark.asyncio
async def test_login_rejects_wrong_key(auth_client):
    resp = await auth_client.post(
        "/api/v1/auth/login", json={"api_key": "wrong"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_login_sets_session_cookie(auth_client):
    resp = await auth_client.post(
        "/api/v1/auth/login", json={"api_key": TEST_API_KEY}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["mode"] == "cookie"
    status_resp = await auth_client.get("/api/v1/status")
    assert status_resp.status_code == 200


@pytest.mark.asyncio
async def test_login_via_header_sets_cookie(auth_client):
    resp = await auth_client.post(
        "/api/v1/auth/login",
        json={"api_key": ""},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "cookie"
    status_resp = await auth_client.get("/api/v1/status")
    assert status_resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_status_reports_mode(auth_client):
    resp = await auth_client.get("/api/v1/auth/status")
    body = resp.json()
    assert body["authenticated"] is False
    assert body["auth_enabled"] is True
    assert body["mode"] == ""

    resp2 = await auth_client.get(
        "/api/v1/auth/status", headers={"X-API-Key": TEST_API_KEY}
    )
    body2 = resp2.json()
    assert body2["authenticated"] is True
    assert body2["mode"] == "header"

    await auth_client.post("/api/v1/auth/login", json={"api_key": TEST_API_KEY})
    resp3 = await auth_client.get("/api/v1/auth/status")
    body3 = resp3.json()
    assert body3["authenticated"] is True
    assert body3["mode"] == "cookie"


@pytest.mark.asyncio
async def test_logout_clears_session(auth_client):
    await auth_client.post("/api/v1/auth/login", json={"api_key": TEST_API_KEY})
    resp = await auth_client.post("/api/v1/auth/logout")
    assert resp.json()["authenticated"] is False
    status = await auth_client.get("/api/v1/status")
    assert status.status_code == 401


@pytest.mark.asyncio
async def test_cookie_rejected_for_wrong_signing_key(auth_client):
    from backend.api.security import SESSION_COOKIE_NAME, issue_session_token

    token = issue_session_token(api_key="signing-key-wrong")
    auth_client.cookies.set(SESSION_COOKIE_NAME, token)
    resp = await auth_client.get("/api/v1/status")
    assert resp.status_code == 401


# ---- Session token hashing / revocation / rotation ----


def test_session_tokens_are_random_opaque_and_hashed():
    from backend.api.security import (
        issue_session_token,
        reset_session_store,
        session_token_valid,
    )

    reset_session_store()
    api_key = "k"
    t1 = issue_session_token(api_key=api_key)
    t2 = issue_session_token(api_key=api_key)
    # Two fresh issues yield distinct opaque tokens (not deterministic).
    assert t1 != t2
    assert "." not in t1  # no embedded expiry/signature is present
    assert len(t1) >= 40
    # Both are valid under the same key.
    assert session_token_valid(t1, api_key=api_key) is True
    assert session_token_valid(t2, api_key=api_key) is True
    reset_session_store()


def test_only_token_hash_is_stored_not_raw_token():
    from backend.api.security import (
        _hash_token,
        _session_store,
        issue_session_token,
        reset_session_store,
    )

    reset_session_store()
    token = issue_session_token(api_key="k")
    # The raw token must never be present in the store — only its SHA-256 digest.
    raw_present = any(token == rec.token_hash for rec in _session_store.values())
    assert raw_present is False
    assert set(_session_store.keys()) == {_hash_token(token)}
    assert _hash_token(token) != token
    reset_session_store()


def test_session_invalid_when_api_key_rotates():
    """Rotating BERU_API_KEY invalidates every outstanding session."""
    from backend.api.security import issue_session_token, reset_session_store, session_token_valid

    reset_session_store()
    token = issue_session_token(api_key="old-key")
    assert session_token_valid(token, api_key="old-key") is True
    # After the key rotates, the same token is no longer valid.
    assert session_token_valid(token, api_key="new-key") is False
    reset_session_store()


def test_revoke_session_invalidates_it():
    from backend.api.security import (
        issue_session_token,
        reset_session_store,
        revoke_session_token,
        session_token_valid,
    )

    reset_session_store()
    token = issue_session_token(api_key="k")
    assert session_token_valid(token, api_key="k") is True
    revoke_session_token(token)
    assert session_token_valid(token, api_key="k") is False
    # Revoking again is a no-op.
    revoke_session_token(token)
    reset_session_store()


def test_revoke_all_sessions_under_key():
    from backend.api.security import (
        issue_session_token,
        reset_session_store,
        revoke_all_sessions,
        session_token_valid,
    )

    reset_session_store()
    a = issue_session_token(api_key="a")
    b = issue_session_token(api_key="b")
    n = revoke_all_sessions(api_key="a")
    assert n == 1
    assert session_token_valid(a, api_key="a") is False
    assert session_token_valid(b, api_key="b") is True
    reset_session_store()


@pytest.mark.asyncio
async def test_logout_revokes_session_server_side(auth_client):
    """After logout the same cookie value no longer authenticates the client."""
    from backend.api.security import SESSION_COOKIE_NAME

    await auth_client.post("/api/v1/auth/login", json={"api_key": TEST_API_KEY})
    assert (await auth_client.get("/api/v1/status")).status_code == 200

    # Capture the exact cookie value then log out.
    token = auth_client.cookies.get(SESSION_COOKIE_NAME)
    assert token
    await auth_client.post("/api/v1/auth/logout")

    # Logout clears the client cookie ...
    assert auth_client.cookies.get(SESSION_COOKIE_NAME) in (None, "", "deleted")
    # ... and the token is revoked server-side, so re-introducing the same
    # (stolen) value no longer authenticates.
    auth_client.cookies.set(SESSION_COOKIE_NAME, token)
    resp = await auth_client.get("/api/v1/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_rotates_session_token(auth_client):
    """Each login mints a fresh random token; the old value stops authenticating."""
    from backend.api.security import SESSION_COOKIE_NAME

    await auth_client.post("/api/v1/auth/login", json={"api_key": TEST_API_KEY})
    first = auth_client.cookies.get(SESSION_COOKIE_NAME)
    assert first

    await auth_client.post("/api/v1/auth/login", json={"api_key": TEST_API_KEY})
    second = auth_client.cookies.get(SESSION_COOKIE_NAME)
    assert second
    # Fresh random token every login.
    assert second != first
    assert (await auth_client.get("/api/v1/status")).status_code == 200


# ---- Newly-protected routers ----


@pytest.mark.asyncio
async def test_facts_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/facts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_facts_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/facts", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_projects_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/projects")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_projects_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/projects", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_plans_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/plans/active")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_plans_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/plans/active", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_sync_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/sync/devices")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_sync_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/sync/devices", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_voice_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/voice/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_voice_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/voice/status", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_browser_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/browser/pages")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_browser_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/browser/pages", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_system_control_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/system-control/apps")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_system_control_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/system-control/apps", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_llm_endpoint_requires_api_key(auth_client):
    resp = await auth_client.get("/api/v1/llm")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_llm_endpoint_accepts_correct_key(auth_client):
    resp = await auth_client.get(
        "/api/v1/llm", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_llm_test_endpoint_requires_api_key(auth_client):
    resp = await auth_client.post("/api/v1/llm/test")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_llm_test_endpoint_accepts_correct_key(auth_client):
    resp = await auth_client.post(
        "/api/v1/llm/test", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200


# ---- websocket_authorized unit tests ----


def _make_ws_and_settings(headers=None, cookies=None, with_key=True):
    class _FakeWS:
        def __init__(self, headers, cookies):
            self.headers = headers
            self.cookies = cookies
    kwargs = {"LLM_PROVIDER": "mock", "HOST": "127.0.0.1"}
    if with_key:
        kwargs["BERU_API_KEY"] = TEST_API_KEY
    settings = Settings(**kwargs)
    return _FakeWS(headers=headers or {}, cookies=cookies or {}), settings


def test_websocket_authorized_when_auth_disabled():
    from backend.api.security import websocket_authorized
    ws, settings = _make_ws_and_settings(with_key=False)
    assert websocket_authorized(ws, settings) is True


def test_websocket_authorized_with_valid_cookie():
    from backend.api.security import issue_session_token, websocket_authorized
    token = issue_session_token(api_key=TEST_API_KEY)
    ws, settings = _make_ws_and_settings(cookies={"beru_session": token})
    assert websocket_authorized(ws, settings) is True


def test_websocket_authorized_with_valid_header():
    from backend.api.security import websocket_authorized
    ws, settings = _make_ws_and_settings(headers={"X-API-Key": TEST_API_KEY})
    assert websocket_authorized(ws, settings) is True


def test_websocket_rejected_without_credentials():
    from backend.api.security import websocket_authorized
    ws, settings = _make_ws_and_settings()
    assert websocket_authorized(ws, settings) is False


def test_websocket_rejected_with_wrong_cookie():
    from backend.api.security import issue_session_token, websocket_authorized
    token = issue_session_token(api_key="wrong-key")
    ws, settings = _make_ws_and_settings(cookies={"beru_session": token})
    assert websocket_authorized(ws, settings) is False
