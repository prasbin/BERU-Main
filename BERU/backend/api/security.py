"""API authentication — owner key + per-user accounts with durable sessions.

BERU authenticates in two complementary ways:

* **Owner API key** (``BERU_API_KEY``): the system-wide credential. When it
  matches (``X-API-Key`` header) the request runs as the **owner**, who sees
  and manages everything — exactly the legacy single-user mode. When the key
  is blank, authentication is disabled entirely (localhost development only;
  ``main.create_app`` refuses to bind a non-localhost interface without a key).
* **User accounts** (Stage 5.1): ``POST /auth/login`` with
  ``username``/``password`` mints a session bound to that user. Non-owner
  users are *scoped* — they only see their own conversations, facts, and
  projects (enforced by the services through :data:`backend.core.context`).

Every protected route requires a valid credential: an owner ``X-API-Key``
header or a ``beru_session`` cookie (browser REST + WebSocket handshakes, where
the raw key must never be placed in a URL or persistent storage). Comparisons
are constant-time so responses cannot leak secrets through timing.

Session tokens are **random, opaque** values (``secrets.token_urlsafe``). Only a
**SHA-256 hash** of each token is retained — never the raw token — alongside its
issue realm (a hash of the API key it was issued under), user, expiry, and
revocation time. Sessions are bound to the API-key generation: rotating
``BERU_API_KEY`` invalidates every outstanding session (implicit rotation), and
each login mints a fresh random token.

The in-memory store is the fast path. When a database is available
(:func:`persist_session_issue`/:func:`persist_session_revoke` at login/logout,
:func:`restore_sessions` at startup) sessions **survive restarts**; otherwise
they are process-local (the safe no-DB default).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Depends, Request, Security, WebSocket
from fastapi.security import APIKeyHeader

from backend.core.config import Settings, get_settings
from backend.core.context import Principal, set_current_principal
from backend.core.errors import AuthenticationError

API_KEY_HEADER_NAME = "X-API-Key"
SESSION_COOKIE_NAME = "beru_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# ``auto_error=False`` so we raise our own AuthenticationError (401) that flows
# through the standard BERU error envelope, rather than FastAPI's default 403.
# Declaring the scheme also surfaces an "Authorize" button in the Swagger UI.
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def _hash_token(token: str) -> str:
    """Return a SHA-256 hex digest of a token/key (never store the raw value)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _SessionRecord:
    """Server-side bookkeeping for one issued session token."""

    token_hash: str
    api_key: str
    expires_at: float
    revoked_at: float | None = None
    user_id: str | None = None
    username: str | None = None


#: Process-local session store keyed by token hash. Kept as a plain dict so it
#: is trivially resettable by tests via :func:`reset_session_store`.
_session_store: dict[str, _SessionRecord] = {}


def _to_dt(value: float) -> datetime:
    """Convert an epoch-seconds float to an aware UTC datetime."""
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _to_epoch(value: datetime) -> float:
    """Convert a (possibly naive-UTC) datetime to epoch seconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _prune_sessions(now: float | None = None) -> None:
    """Drop revoked or fully-expired entries to bound memory growth."""
    now = time.time() if now is None else now
    stale = [
        h
        for h, rec in _session_store.items()
        if rec.expires_at <= now
        or (rec.revoked_at is not None and now - rec.revoked_at > SESSION_TTL_SECONDS)
    ]
    for h in stale:
        _session_store.pop(h, None)


def issue_session_token(
    *,
    api_key: str,
    user_id: str | None = None,
    username: str | None = None,
    ttl_seconds: float = SESSION_TTL_SECONDS,
) -> str:
    """Issue a random, opaque session token bound to ``api_key``.

    Only the SHA-256 digest is stored; the raw token is returned to the caller
    (and placed in the ``HttpOnly`` cookie) and is never persisted. When
    ``user_id`` is given the session resolves to that user's scope (a scoped
    non-owner principal); otherwise it resolves to the owner.
    """
    token = secrets.token_urlsafe(32)
    _prune_sessions()
    _session_store[_hash_token(token)] = _SessionRecord(
        token_hash=_hash_token(token),
        api_key=api_key,
        expires_at=time.time() + ttl_seconds,
        user_id=user_id,
        username=username,
    )
    return token


def session_token_valid(token: str | None, *, api_key: str) -> bool:
    """True when ``token`` is a valid, unexpired, non-revoked session for ``api_key``.

    The token must have been issued under this exact ``api_key`` (so rotating
    the key invalidates every outstanding session) and must not have been
    revoked by :func:`revoke_session_token`.
    """
    return resolve_principal_from_session(token, api_key=api_key) is not None


def resolve_principal_from_session(
    token: str | None, *, api_key: str
) -> Principal | None:
    """Resolve a session token to its :class:`Principal`, or None if invalid.

    Owner-issued sessions resolve to an owner (unscoped) principal; sessions
    bound to a user resolve to that user's scoped principal.
    """
    if not token:
        return None
    rec = _session_store.get(_hash_token(token))
    if rec is None:
        return None
    if rec.revoked_at is not None:
        return None
    if time.time() >= rec.expires_at:
        _session_store.pop(rec.token_hash, None)
        return None
    if not secrets.compare_digest(rec.api_key, api_key):
        return None
    if rec.user_id:
        return Principal(
            user_id=rec.user_id,
            username=rec.username,
            is_owner=False,
            auth_enabled=True,
            mode="cookie",
        )
    return Principal(is_owner=True, auth_enabled=True, mode="cookie")


def revoke_session_token(token: str | None) -> None:
    """Revoke a session server-side so it can no longer be used.

    Called on logout; the cookie is also deleted from the client, but the
    server-side revocation is what actually invalidates a stolen cookie.
    """
    if not token:
        return
    rec = _session_store.get(_hash_token(token))
    if rec is not None and rec.revoked_at is None:
        rec.revoked_at = time.time()


def revoke_all_sessions(api_key: str | None = None) -> int:
    """Revoke every session (optionally only those issued under ``api_key``).

    Returns the number of sessions revoked. Used for global rotation.
    """
    now = time.time()
    revoked = 0
    for rec in list(_session_store.values()):
        if api_key is not None and rec.api_key != api_key:
            continue
        if rec.revoked_at is None:
            rec.revoked_at = now
            revoked += 1
    return revoked


def reset_session_store() -> None:
    """Clear the session store (test isolation)."""
    _session_store.clear()


def session_count() -> int:
    """Number of currently valid (unexpired, unrevoked) sessions."""
    _prune_sessions()
    return sum(1 for rec in _session_store.values() if rec.revoked_at is None)


# ---- Durable (DB-backed) session persistence ------------------------------


async def persist_session_issue(
    session,
    token: str,
    *,
    api_key: str,
) -> None:
    """Write an issued session row to the database (best effort).

    Call after :func:`issue_session_token` so the session survives restarts.
    Uses the caller's request-scoped async ``session``; a failure is logged but
    never fails the login itself (the in-memory session still works this run).
    """
    rec = _session_store.get(_hash_token(token))
    if rec is None:  # pragma: no cover - issued tokens are always stored
        return
    try:
        from backend.models.session import SessionRecord

        session.add(
            SessionRecord(
                token_hash=rec.token_hash,
                api_key_hash=_hash_token(api_key),
                user_id=rec.user_id,
                expires_at=_to_dt(rec.expires_at),
            )
        )
        await session.commit()
    except Exception:
        from backend.core.logging import get_logger

        get_logger(__name__).exception("Failed to persist session issue")


async def persist_session_revoke(session, token: str) -> None:
    """Mark an issued session revoked in the database (best effort).

    Call after :func:`revoke_session_token` on logout so the revocation
    survives restarts too.
    """
    rec = _session_store.get(_hash_token(token))
    if rec is None or rec.revoked_at is None:
        return
    try:
        from sqlalchemy import select

        from backend.models.session import SessionRecord

        row = (
            await session.execute(
                select(SessionRecord).where(
                    SessionRecord.token_hash == rec.token_hash
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.revoked_at = _to_dt(rec.revoked_at)
            await session.commit()
    except Exception:
        from backend.core.logging import get_logger

        get_logger(__name__).exception("Failed to persist session revocation")


async def restore_sessions(
    sessionmaker=None, *, api_key: str | None = None
) -> int:
    """Load all current, unrevoked, same-realm sessions from the DB into memory.

    Rows whose ``api_key_hash`` does not match the current API key (the realm
    that issued them) are skipped, preserving rotation semantics across
    restarts: rotating the key invalidates stored sessions too.

    Returns the number of sessions restored.
    """
    if sessionmaker is None:
        from backend.database.base import get_sessionmaker

        sessionmaker = get_sessionmaker()
    if api_key is None:
        api_key = get_settings().api_key
    realm_hash = _hash_token(api_key)
    restored = 0
    try:
        from sqlalchemy import orm, select

        from backend.models.session import SessionRecord

        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    select(SessionRecord)
                    .options(orm.selectinload(SessionRecord.user))
                    .where(SessionRecord.api_key_hash == realm_hash)
                )
            ).scalars().all()
            now = time.time()
            for row in rows:
                if (
                    row.revoked_at is None
                    and _to_epoch(row.expires_at) > now
                ):
                    username = row.user.username if row.user is not None else None
                    _session_store[row.token_hash] = _SessionRecord(
                        token_hash=row.token_hash,
                        api_key=api_key,
                        expires_at=_to_epoch(row.expires_at),
                        user_id=row.user_id,
                        username=username,
                    )
                    restored += 1
    except Exception:
        from backend.core.logging import get_logger

        get_logger(__name__).exception("Failed to restore sessions from database")
    return restored


def _make_principal(
    *,
    is_owner: bool,
    user_id: str | None = None,
    username: str | None = None,
    mode: str,
) -> Principal:
    return Principal(
        user_id=user_id,
        username=username,
        is_owner=is_owner,
        auth_enabled=True,
        mode=mode,
    )


async def require_api_key(
    request: Request,
    provided_key: str | None = Security(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Enforce authentication on a route; returns the request :class:`Principal`.

    The resolved principal is also stored in the per-request context so
    services can scope their queries (see :mod:`backend.core.context`).

    A no-op when auth is disabled (no key configured) — the system/owner
    principal is used. Otherwise the request must carry a valid credential —
    an ``X-API-Key`` header or a valid session cookie — compared/verified
    without leaking the secret.
    """
    if not settings.auth_enabled:
        from backend.core.context import SYSTEM_PRINCIPAL

        set_current_principal(SYSTEM_PRINCIPAL)
        return SYSTEM_PRINCIPAL
    if provided_key and secrets.compare_digest(provided_key, settings.api_key):
        principal = _make_principal(is_owner=True, mode="header")
        set_current_principal(principal)
        return principal
    principal = resolve_principal_from_session(
        request.cookies.get(SESSION_COOKIE_NAME), api_key=settings.api_key
    )
    if principal is not None:
        set_current_principal(principal)
        return principal
    raise AuthenticationError("Missing or invalid API key.")


async def require_owner(
    request: Request,
    principal: Principal = Depends(require_api_key),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Like :func:`require_api_key` but additionally requires owner privileges.

    Non-owner (scoped) users get a 401 here, so account management endpoints
    are only reachable by the owner.
    """
    if settings.auth_enabled and not principal.is_owner:
        raise AuthenticationError("Owner privileges required.")
    return principal


def websocket_authorized(websocket: WebSocket, settings: Settings) -> bool:
    """Decide whether a WebSocket handshake may proceed.

    Honours the same auth model as :func:`require_api_key` for the upgrade
    request: browsers attach the session cookie automatically, and non-browser
    clients (tests, tooling) may send ``X-API-Key`` — never a key in the URL.
    The resolved principal is stored in the connection's task context.
    """
    if not settings.auth_enabled:
        from backend.core.context import SYSTEM_PRINCIPAL

        set_current_principal(SYSTEM_PRINCIPAL)
        return True
    header_key = websocket.headers.get(API_KEY_HEADER_NAME)
    if header_key and secrets.compare_digest(header_key, settings.api_key):
        set_current_principal(_make_principal(is_owner=True, mode="header"))
        return True
    principal = resolve_principal_from_session(
        websocket.cookies.get(SESSION_COOKIE_NAME), api_key=settings.api_key
    )
    if principal is not None:
        set_current_principal(principal)
        return True
    return False


def websocket_origin_ok(websocket: WebSocket, settings: Settings) -> bool:
    """Validate the browser ``Origin`` on a WebSocket upgrade.

    Cookies travel across any port on the same host, so a malicious page served
    on the same machine (e.g. from a dev server) could otherwise use the session
    cookie to hijack the WS channel. The rule:

      * No ``Origin`` header (non-browser clients) is allowed — they carry an
        explicit key, never cookies.
      * An explicit CORS origin configured via ``CORS_ORIGINS`` is allowed.
      * Otherwise Origin must match the request's own Host (host *and* port) —
        i.e. a page served by BERU itself or on the exact same endpoint.

    A wildcard ``CORS_ORIGINS`` never grants WS access; it only permits
    unauthenticated header-based CORS on the HTTP API.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False

    origins = settings.cors_origin_list
    if "*" not in origins:
        normalized = {o.rstrip("/") for o in origins if o and o.rstrip("/") != "*"}
        if origin.rstrip("/") in normalized:
            return True

    host = websocket.headers.get("host")
    if not host:
        return False
    try:
        host_name, _, host_port = host.partition(":")
        port = int(host_port) if host_port else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return parsed.hostname.lower() == host_name.lower() and (parsed.port or port) == port