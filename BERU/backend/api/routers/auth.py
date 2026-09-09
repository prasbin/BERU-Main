"""Auth endpoints — establish, check, and end a browser session; manage accounts.

The browser never stores the raw API key in localStorage, URLs, or logs: it
sends it once to ``POST /auth/login`` whose valid-submission response sets an
``HttpOnly`` ``beru_session`` cookie. That cookie is then attached to every
subsequent REST call and WebSocket handshake automatically. When auth is
disabled (no ``BERU_API_KEY``) the endpoints report ``mode: "none"`` and treat
the system as open — for localhost development only.

Stage 5.1 adds accounts: ``POST /auth/login`` also accepts
``username``/``password``, minting a session scoped to that user. Account
management (``POST``/``GET /auth/users``) is owner-only.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.rate_limit import rate_limit_auth
from backend.api.security import (
    API_KEY_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    issue_session_token,
    persist_session_issue,
    persist_session_revoke,
    require_owner,
    resolve_principal_from_session,
    revoke_session_token,
)
from backend.core.config import get_settings
from backend.core.context import Principal
from backend.core.errors import AuthenticationError
from backend.database.base import get_session
from backend.services.user_service import (
    authenticate_user,
    create_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    api_key: str = ""
    username: str = ""
    password: str = ""


class AuthStatus(BaseModel):
    authenticated: bool
    auth_enabled: bool
    mode: str = ""
    username: str | None = None
    role: str = ""


class UserCreate(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    is_owner: bool


@router.post(
    "/login",
    response_model=AuthStatus,
    summary="Authenticate and establish a session",
    dependencies=[Depends(rate_limit_auth)],
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AuthStatus:
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthStatus(authenticated=True, auth_enabled=False, mode="none")

    header_key = request.headers.get(API_KEY_HEADER_NAME)
    header_ok = bool(header_key) and secrets.compare_digest(header_key, settings.api_key)
    body_ok = bool(body.api_key) and secrets.compare_digest(body.api_key, settings.api_key)

    if header_ok or body_ok:
        token = issue_session_token(api_key=settings.api_key)
        await persist_session_issue(session, token, api_key=settings.api_key)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
            secure=request.url.scheme == "https",
        )
        return AuthStatus(
            authenticated=True, auth_enabled=True, mode="cookie", role="owner"
        )

    if body.username:
        user = await authenticate_user(
            session, body.username.strip(), body.password
        )
        if user is not None:
            token = issue_session_token(
                api_key=settings.api_key,
                user_id=user.id,
                username=user.username,
            )
            await persist_session_issue(session, token, api_key=settings.api_key)
            response.set_cookie(
                SESSION_COOKIE_NAME,
                token,
                max_age=SESSION_TTL_SECONDS,
                httponly=True,
                samesite="lax",
                path="/",
                secure=request.url.scheme == "https",
            )
            return AuthStatus(
                authenticated=True,
                auth_enabled=True,
                mode="cookie",
                username=user.username,
                role="owner" if user.is_owner else "user",
            )
        raise AuthenticationError("Invalid credentials.")

    raise AuthenticationError("Invalid credentials.")


@router.post(
    "/logout",
    response_model=AuthStatus,
    summary="End the current session",
)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AuthStatus:
    settings = get_settings()
    # Revoke the session server-side first, so a stolen cookie is invalidated
    # immediately rather than lingering until its TTL. The client cookie is
    # then also cleared, and the revocation is persisted for restarts.
    token = request.cookies.get(SESSION_COOKIE_NAME)
    revoke_session_token(token)
    await persist_session_revoke(session, token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return AuthStatus(
        authenticated=False,
        auth_enabled=settings.auth_enabled,
        mode="" if settings.auth_enabled else "none",
    )


@router.get(
    "/status",
    response_model=AuthStatus,
    summary="Report the current authentication state",
)
async def auth_status(request: Request) -> AuthStatus:
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthStatus(authenticated=True, auth_enabled=False, mode="none")

    header_key = request.headers.get(API_KEY_HEADER_NAME)
    if header_key and secrets.compare_digest(header_key, settings.api_key):
        return AuthStatus(
            authenticated=True, auth_enabled=True, mode="header", role="owner"
        )
    principal = resolve_principal_from_session(
        request.cookies.get(SESSION_COOKIE_NAME), api_key=settings.api_key
    )
    if principal is not None:
        return AuthStatus(
            authenticated=True,
            auth_enabled=True,
            mode=principal.mode,
            username=principal.username,
            role="owner" if principal.is_owner else "user",
        )
    return AuthStatus(authenticated=False, auth_enabled=True, mode="")


@router.post(
    "/users",
    response_model=UserOut,
    summary="Create a user account (owner only)",
)
async def create_account(
    body: UserCreate,
    principal: Principal = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await create_user(
        session, username=body.username, password=body.password, is_owner=False
    )
    return UserOut(id=user.id, username=user.username, is_owner=user.is_owner)


@router.get(
    "/users",
    response_model=list[UserOut],
    summary="List user accounts (owner only)",
)
async def list_accounts(
    principal: Principal = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
) -> list[UserOut]:
    from sqlalchemy import select

    from backend.models.user import User

    users = (
        await session.execute(select(User).order_by(User.username))
    ).scalars().all()
    return [
        UserOut(id=u.id, username=u.username, is_owner=u.is_owner) for u in users
    ]