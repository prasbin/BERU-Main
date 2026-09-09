"""Release-hardening security tests: CORS, WebSocket origin, login rate limit,
rate-limiter proxy-header handling, and provider-error sanitization."""

from __future__ import annotations

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.rate_limit import RateLimiter, get_auth_limiter
from backend.core.config import Settings, get_settings
from backend.engines.llm.registry import reset_llm_provider
from backend.main import create_app


def _app(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_llm_provider()
    return create_app()


def _middleware_kwargs(app, middleware_cls):
    for middleware in app.user_middleware:
        if middleware.cls is middleware_cls:
            return middleware.kwargs
    return None


def test_cors_wildcard_disables_credentials(monkeypatch):
    """A '*' CORS origin must never be combined with allow_credentials=True."""
    from fastapi.middleware.cors import CORSMiddleware

    app = _app(monkeypatch, CORS_ORIGINS="*")
    kwargs = _middleware_kwargs(app, CORSMiddleware)
    assert kwargs is not None
    assert "*" in kwargs["allow_origins"]
    assert kwargs.get("allow_credentials") is False


def test_cors_explicit_origins_enable_credentials(monkeypatch):
    from fastapi.middleware.cors import CORSMiddleware

    app = _app(monkeypatch, CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173")
    kwargs = _middleware_kwargs(app, CORSMiddleware)
    assert kwargs is not None
    assert "*" not in kwargs["allow_origins"]
    assert kwargs.get("allow_credentials") is True


def test_security_headers_present(monkeypatch):
    """The hardening headers are applied to every response."""
    app = _app(monkeypatch)  # auth off, localhost bound
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert "default-src 'self'" in resp.headers["content-security-policy"]


def test_websocket_accepts_trusted_origin(monkeypatch):
    app = _app(monkeypatch)  # auth disabled -> ws authorized
    client = TestClient(app)
    with client.websocket_connect(
        "/ws/client_ok", headers={"origin": "http://testserver"}
    ) as ws:
        assert ws is not None


def test_websocket_rejects_cross_origin(monkeypatch):
    app = _app(monkeypatch)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/client_ok", headers={"origin": "http://evil.example"}
        ):
            pass
    assert exc_info.value.code == 1008


def test_websocket_rejects_malformed_client_id(monkeypatch):
    app = _app(monkeypatch)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/bad%20client%21", headers={"origin": "http://testserver"}
        ):
            pass
    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_login_endpoint_is_rate_limited(monkeypatch):
    """Brute-forcing /auth/login trips the per-IP limiter (429)."""
    from httpx import ASGITransport, AsyncClient

    app = _app(monkeypatch, BERU_API_KEY="secret-test-key", HOST="127.0.0.1")
    override_limiter = RateLimiter(rpm=2, burst=1)

    async def _override_limiter(settings: Settings = Depends(get_settings)):
        return override_limiter

    app.dependency_overrides[get_auth_limiter] = _override_limiter

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=True
    ) as c:
        r1 = await c.post("/api/v1/auth/login", json={"api_key": "secret-test-key"})
        r2 = await c.post("/api/v1/auth/login", json={"api_key": "secret-test-key"})
        r3 = await c.post("/api/v1/auth/login", json={"api_key": "wrong-key"})
        r4 = await c.post("/api/v1/auth/login", json={"api_key": "secret-test-key"})
    assert [r1.status_code, r2.status_code] == [200, 200]
    assert [r3.status_code, r4.status_code] == [429, 429]
    assert r4.json()["error"]["type"] == "rate_limit_exceeded"
    assert r3.json()["error"]["detail"]["retry_after_seconds"] >= 1


@pytest.mark.asyncio
async def test_xff_ignored_unless_trust_proxy_headers(monkeypatch):
    """Spoofed X-Forwarded-For must not splinter the rate-limit buckets."""
    from httpx import ASGITransport, AsyncClient

    app = _app(monkeypatch, LLM_PROVIDER="mock")
    override_limiter = RateLimiter(rpm=2, burst=1)

    async def _override_limiter(settings: Settings = Depends(get_settings)):
        return override_limiter

    app.dependency_overrides[get_auth_limiter] = _override_limiter

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        statuses = []
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            resp = await c.post(
                "/api/v1/auth/login",
                json={"api_key": ""},
                headers={"x-forwarded-for": ip},
            )
            statuses.append(resp.status_code)
    # All three funnel into ONE bucket (testserver): the third request is 429.
    assert statuses == [200, 200, 429]


@pytest.mark.asyncio
async def test_xff_honored_when_proxy_headers_trusted(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    app = _app(
        monkeypatch,
        LLM_PROVIDER="mock",
        BERU_RATE_LIMIT_AUTH_RPM="5",
        BERU_TRUST_PROXY_HEADERS="true",
    )
    override_limiter = RateLimiter(rpm=2, burst=1)

    async def _override_limiter(settings: Settings = Depends(get_settings)):
        return override_limiter

    app.dependency_overrides[get_auth_limiter] = _override_limiter

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        statuses = []
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            resp = await c.post(
                "/api/v1/auth/login",
                json={"api_key": ""},
                headers={"x-forwarded-for": ip},
            )
            statuses.append(resp.status_code)
    # Each XFF lands in its own bucket (rpm=2) -> none are blocked.
    assert statuses == [200, 200, 200]


@pytest.mark.asyncio
async def test_llm_provider_error_response_is_sanitized(monkeypatch):
    """Provider internals must not leak to the client through /chat."""
    from fastapi import Request

    from backend.core.errors import LLMProviderError, _handle_beru_error

    exc = LLMProviderError("connection refused to api.openai.example:443")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat",
            "url": "http://test/api/v1/chat",
            "headers": [],
        }
    )
    result = await _handle_beru_error(request, exc)
    assert result is not None  # handled -> returns a JSONResponse
    body = result.body.decode()
    assert "connection refused" not in body
    assert "llm_provider_error" in body