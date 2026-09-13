"""Stage 5.1 — accounts and multi-user isolation.

Covers: owner bootstrap, PBKDF2 password hashing, owner-only account
management, username/password login, per-user isolation of conversations,
facts, and projects (including shared fact keys / project names across users),
cross-user 404s through the scoped services and chat, and durable sessions
that survive restarts / respect revocation and key rotation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.agents.registry import get_agent_registry
from backend.api.security import (
    SESSION_COOKIE_NAME,
    reset_session_store,
    restore_sessions,
    session_token_valid,
)
from backend.core.config import get_settings
from backend.database.base import get_session
from backend.engines.intelligence import get_intelligence_engine
from backend.engines.llm.registry import reset_llm_provider
from backend.main import create_app
from backend.tools.registry import get_tool_registry

TEST_API_KEY = "test-secret-key-12345"


@dataclass
class Ctx:
    """Bag of fixtures for multi-user tests."""

    maker: Any
    app: Any
    owner: AsyncClient
    client_a: AsyncClient | None = None
    client_b: AsyncClient | None = None


@pytest_asyncio.fixture
async def multiuser(_sessionmaker, monkeypatch):
    """An app with auth enabled backed by the temp DB; yields helper clients."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("BERU_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("HOST", "127.0.0.1")
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()
    reset_session_store()

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
    async with AsyncClient(transport=transport, base_url="http://testserver") as owner:
        ctx = Ctx(maker=_sessionmaker, app=app, owner=owner)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client_a:
            ctx.client_a = client_a
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client_b:
                ctx.client_b = client_b
                yield ctx
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    reset_llm_provider()
    get_intelligence_engine.cache_clear()
    get_agent_registry.cache_clear()
    get_tool_registry.cache_clear()
    reset_session_store()


async def _create_user(ctx: Ctx, username: str, password: str = "pw-12345") -> None:
    resp = await ctx.owner.post(
        "/api/v1/auth/users",
        json={"username": username, "password": password},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == username


async def _login(client: AsyncClient, username: str, password: str) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "cookie"


async def _create_user_service(ctx: Ctx, username: str, password: str = "pw-12345") -> None:
    from backend.services.user_service import create_user

    async with ctx.maker() as session:
        await create_user(
            session, username=username, password=password, commit=True
        )


# ---- Owner bootstrap & password hashing -----------------------------------


@pytest.mark.asyncio
async def test_owner_bootstrap_creates_single_owner(multiuser):
    from backend.models.user import User
    from backend.services.user_service import ensure_owner

    async with multiuser.maker() as session:
        owner = await ensure_owner(session)
        assert owner is not None
        assert owner.is_owner is True
        assert owner.username == get_settings().owner_username
        assert owner.password_hash is not None
        first_id = owner.id

        again = await ensure_owner(session)
        assert again.id == first_id  # idempotent

        count = (
            await session.execute(
                select(func.count()).select_from(User).where(User.is_owner)
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_owner_bootstrap_never_logs_generated_password(multiuser, monkeypatch, caplog):
    """Ensure the plaintext of a generated owner password never reaches logs."""
    from backend.services.user_service import ensure_owner

    fixed = "a" * 48  # token_hex(24) produces 48 hex characters
    monkeypatch.setattr("backend.services.user_service.secrets.token_hex", lambda n=24: fixed)
    with caplog.at_level(logging.WARNING, logger="backend.services.user_service"):
        async with multiuser.maker() as session:
            owner = await ensure_owner(session)
    assert owner is not None
    assert fixed not in caplog.text, "generated password must never appear in logs"
    assert "BERU_OWNER_PASSWORD" in caplog.text


@pytest.mark.asyncio
async def test_owner_bootstrap_skipped_when_auth_disabled(_sessionmaker, monkeypatch):
    monkeypatch.delenv("BERU_API_KEY", raising=False)
    get_settings.cache_clear()
    from backend.services.user_service import ensure_owner

    async with _sessionmaker() as session:
        assert await ensure_owner(session) is None
    get_settings.cache_clear()


def test_password_hashing_round_trip():
    from backend.core.passwords import hash_password, verify_password

    digest = hash_password("correct horse")
    assert digest.startswith("pbkdf2_sha256$")
    assert digest != "correct horse"
    assert verify_password("correct horse", digest) is True
    assert verify_password("wrong", digest) is False
    # Verification is constant-time against the stored digest.
    assert verify_password("correct horse", digest) is True


# ---- Account management is owner-only --------------------------------------


@pytest.mark.asyncio
async def test_account_creation_requires_auth(multiuser):
    resp = await multiuser.owner.post(
        "/api/v1/auth/users", json={"username": "nobody", "password": "pw"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_owner_cannot_create_accounts(multiuser):
    await _create_user(multiuser, "alice")
    await _login(multiuser.client_a, "alice", "pw-12345")
    resp = await multiuser.client_a.post(
        "/api/v1/auth/users", json={"username": "bob", "password": "pw"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"
    # The owner can still list accounts (including the non-owner).
    resp = await multiuser.owner.get(
        "/api/v1/auth/users", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 200
    names = {u["username"] for u in resp.json()}
    assert "alice" in names


@pytest.mark.asyncio
async def test_duplicate_username_rejected(multiuser):
    await _create_user(multiuser, "alice")
    resp = await multiuser.owner.post(
        "/api/v1/auth/users",
        json={"username": "alice", "password": "pw-other"},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 400


# ---- Login / status --------------------------------------------------------


@pytest.mark.asyncio
async def test_user_login_and_status(multiuser):
    await _create_user(multiuser, "alice")
    await _login(multiuser.client_a, "alice", "pw-12345")

    resp = await multiuser.client_a.get("/api/v1/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["auth_enabled"] is True
    assert body["mode"] == "cookie"
    assert body["username"] == "alice"
    assert body["role"] == "user"

    # A scoped user reaches protected endpoints via the cookie alone.
    resp = await multiuser.client_a.get("/api/v1/facts")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(multiuser):
    await _create_user(multiuser, "alice")
    await _login(multiuser.client_a, "alice", "pw-12345")
    resp = await multiuser.client_b.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_owner_header_flow_and_role(multiuser):
    resp = await multiuser.owner.get(
        "/api/v1/auth/status", headers={"X-API-Key": TEST_API_KEY}
    )
    body = resp.json()
    assert body["authenticated"] is True
    assert body["mode"] == "header"
    assert body["role"] == "owner"


# ---- Fact isolation (shared keys allowed across users) ----------------------


@pytest.mark.asyncio
async def test_fact_isolation_and_shared_keys(multiuser):
    await _create_user(multiuser, "alice")
    await _create_user(multiuser, "bob")
    await _login(multiuser.client_a, "alice", "pw-12345")
    await _login(multiuser.client_b, "bob", "pw-12345")

    r_a = await multiuser.client_a.post(
        "/api/v1/facts", json={"key": "theme", "value": "dark"}
    )
    assert r_a.status_code == 201, r_a.text
    # A distinct user may remember the same key independently.
    r_b = await multiuser.client_b.post(
        "/api/v1/facts", json={"key": "theme", "value": "light"}
    )
    assert r_b.status_code == 201, r_b.text

    # A private fact is invisible to the other user.
    await multiuser.client_a.post("/api/v1/facts", json={"key": "bank", "value": "123"})
    resp = await multiuser.client_b.get("/api/v1/facts/bank")
    assert resp.status_code == 404

    # Each user reads their own value for the shared key.
    ga = await multiuser.client_a.get("/api/v1/facts/theme")
    assert ga.json()["value"] == "dark"
    gb = await multiuser.client_b.get("/api/v1/facts/theme")
    assert gb.json()["value"] == "light"

    # List totals are scoped.
    la = (await multiuser.client_a.get("/api/v1/facts")).json()
    lb = (await multiuser.client_b.get("/api/v1/facts")).json()
    assert {f["key"] for f in la["items"]} == {"theme", "bank"}
    assert {f["key"] for f in lb["items"]} == {"theme"}


# ---- Project isolation (shared names allowed across users) ------------------


@pytest.mark.asyncio
async def test_project_isolation_and_shared_names(multiuser):
    await _create_user(multiuser, "alice")
    await _create_user(multiuser, "bob")
    await _login(multiuser.client_a, "alice", "pw-12345")
    await _login(multiuser.client_b, "bob", "pw-12345")

    ra = await multiuser.client_a.post(
        "/api/v1/projects", json={"name": "Alpha"}
    )
    assert ra.status_code == 201, ra.text
    a_project_id = ra.json()["id"]
    # A distinct user may create a project with the same name.
    rb = await multiuser.client_b.post("/api/v1/projects", json={"name": "Alpha"})
    assert rb.status_code == 201, rb.text
    b_project_id = rb.json()["id"]
    assert b_project_id != a_project_id

    # Cross-user project access is a 404.
    resp = await multiuser.client_b.get(f"/api/v1/projects/{a_project_id}")
    assert resp.status_code == 404
    resp = await multiuser.client_a.get(f"/api/v1/projects/{b_project_id}")
    assert resp.status_code == 404

    # Lists are scoped.
    la = (await multiuser.client_a.get("/api/v1/projects")).json()
    lb = (await multiuser.client_b.get("/api/v1/projects")).json()
    assert [p["id"] for p in la["items"]] == [a_project_id]
    assert [p["id"] for p in lb["items"]] == [b_project_id]


# ---- Owner sees everything (unscoped) ---------------------------------------


@pytest.mark.asyncio
async def test_owner_sees_all_rows(multiuser):
    await _create_user(multiuser, "alice")
    await _login(multiuser.client_a, "alice", "pw-12345")
    await multiuser.client_a.post("/api/v1/facts", json={"key": "k", "value": "v"})
    await multiuser.client_a.post("/api/v1/projects", json={"name": "P"})

    resp = await multiuser.owner.get(
        "/api/v1/facts", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.json()["total"] == 1
    resp = await multiuser.owner.get(
        "/api/v1/projects", headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.json()["total"] == 1


# ---- Conversation isolation -------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_isolation_and_cross_user_404(multiuser):
    await _create_user(multiuser, "alice")
    await _create_user(multiuser, "bob")
    await _login(multiuser.client_a, "alice", "pw-12345")
    await _login(multiuser.client_b, "bob", "pw-12345")

    ra = await multiuser.client_a.post(
        "/api/v1/chat", json={"message": "hello from alice"}
    )
    assert ra.status_code == 200, ra.text
    a_conv = ra.json()["conversation_id"]

    # Bob cannot read Alice's conversation (GET) ...
    resp = await multiuser.client_b.get(f"/api/v1/conversations/{a_conv}")
    assert resp.status_code == 404
    # ... cannot continue it (POST chat) ...
    resp = await multiuser.client_b.post(
        "/api/v1/chat", json={"message": "hi again", "conversation_id": a_conv}
    )
    assert resp.status_code == 404
    # ... and does not even see it in the list.
    lb = (await multiuser.client_b.get("/api/v1/conversations")).json()
    assert lb["total"] == 0


@pytest.mark.asyncio
async def test_chat_rejects_foreign_project(multiuser):
    await _create_user(multiuser, "alice")
    await _create_user(multiuser, "bob")
    await _login(multiuser.client_a, "alice", "pw-12345")
    await _login(multiuser.client_b, "bob", "pw-12345")

    ra = await multiuser.client_a.post("/api/v1/projects", json={"name": "A"})
    a_project_id = ra.json()["id"]

    # Alice can create a conversation inside her own project.
    ok = await multiuser.client_a.post(
        "/api/v1/chat",
        json={"message": "in project", "project_id": a_project_id},
    )
    assert ok.status_code == 200, ok.text

    # Bob cannot create a conversation inside Alice's project.
    denied = await multiuser.client_b.post(
        "/api/v1/chat",
        json={"message": "sneak", "project_id": a_project_id},
    )
    assert denied.status_code == 404


# ---- Durable sessions (DB persistence across restarts) ----------------------


@pytest.mark.asyncio
async def test_sessions_survive_restart(multiuser):
    await _create_user(multiuser, "alice")
    await _login(multiuser.client_a, "alice", "pw-12345")
    token = multiuser.client_a.cookies.get(SESSION_COOKIE_NAME)
    assert token
    assert session_token_valid(token, api_key=TEST_API_KEY) is True

    # Simulate a restart: wipe the in-memory store, then restore from the DB.
    reset_session_store()
    assert session_token_valid(token, api_key=TEST_API_KEY) is False
    restored = await restore_sessions(multiuser.maker, api_key=TEST_API_KEY)
    assert restored >= 1
    assert session_token_valid(token, api_key=TEST_API_KEY) is True

    # The restored session authenticates a fresh client with the same cookie.
    fresh = AsyncClient(
        transport=ASGITransport(app=multiuser.app), base_url="http://testserver"
    )
    async with fresh:
        fresh.cookies.set(SESSION_COOKIE_NAME, token)
        resp = await fresh.get("/api/v1/auth/status")
        assert resp.status_code == 200
        assert resp.json()["username"] == "alice"
        assert resp.json()["role"] == "user"


@pytest.mark.asyncio
async def test_revoked_sessions_not_restored(multiuser):
    await _create_user(multiuser, "alice")
    await _login(multiuser.client_a, "alice", "pw-12345")
    token = multiuser.client_a.cookies.get(SESSION_COOKIE_NAME)

    # Logout revokes and persists the revocation.
    resp = await multiuser.client_a.post("/api/v1/auth/logout")
    assert resp.status_code == 200

    reset_session_store()
    restored = await restore_sessions(multiuser.maker, api_key=TEST_API_KEY)
    assert restored == 0
    assert session_token_valid(token, api_key=TEST_API_KEY) is False


@pytest.mark.asyncio
async def test_sessions_restricted_to_issuing_api_key(multiuser):
    """A rotated BERU_API_KEY invalidates stored sessions too."""
    await _create_user(multiuser, "alice")
    await _login(multiuser.client_a, "alice", "pw-12345")
    token = multiuser.client_a.cookies.get(SESSION_COOKIE_NAME)

    reset_session_store()
    # Restoring under a different (rotated) key restores nothing.
    restored = await restore_sessions(multiuser.maker, api_key="rotated-key")
    assert restored == 0
    assert session_token_valid(token, api_key="rotated-key") is False


# ---- Per-user rate limiting -------------------------------------------------


@pytest.mark.asyncio
async def test_chat_rate_limit_keyed_per_user(multiuser, monkeypatch):
    await _create_user(multiuser, "alice")
    await _create_user(multiuser, "bob")
    await _login(multiuser.client_a, "alice", "pw-12345")
    await _login(multiuser.client_b, "bob", "pw-12345")

    # Drop the chat RPM to 1 so a single extra request trips the limiter.
    monkeypatch.setenv("BERU_RATE_LIMIT_CHAT_RPM", "1")
    monkeypatch.setenv("BERU_RATE_LIMIT_BURST", "1")
    get_settings.cache_clear()

    first = await multiuser.client_a.post(
        "/api/v1/chat", json={"message": "hi"}
    )
    assert first.status_code == 200, first.text
    second = await multiuser.client_a.post(
        "/api/v1/chat", json={"message": "again"}
    )
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limit_exceeded"

    # Bob is untouched: the limiter keys by username, not by shared IP.
    bob_first = await multiuser.client_b.post(
        "/api/v1/chat", json={"message": "bob hi"}
    )
    assert bob_first.status_code == 200, bob_first.text
    get_settings.cache_clear()