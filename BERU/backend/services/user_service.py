"""User account service (Stage 5.1) — account management and auth checks.

Accounts are created and managed by the owner (``get_settings().api_key`` /
owner principal). Ordinary users are *scoped*: they only see their own
conversations, facts, and projects (see :mod:`backend.services.scoping` and the
per-service filters).
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select

from backend.core.config import get_settings
from backend.core.errors import BadRequestError
from backend.core.passwords import hash_password, verify_password
from backend.models.user import User

logger = logging.getLogger(__name__)


async def create_user(
    session,
    *,
    username: str,
    password: str | None,
    is_owner: bool = False,
    commit: bool = True,
) -> User:
    """Create a user account (owner-only through the API).

    ``username`` must be unique; ``password`` must be non-blank for ordinary
    users; ``is_owner`` accounts can only be created by :func:`ensure_owner` —
    the API refuses to create additional owners (only one superuser, ever).
    """
    if not username or not username.strip():
        raise BadRequestError("Username must not be blank.")
    username = username.strip()
    if len(username) > 64:
        raise BadRequestError("Username must be 64 characters or fewer.")
    if not is_owner and (not password or not password.strip()):
        raise BadRequestError("Password must not be blank.")

    existing = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if existing is not None:
        raise BadRequestError("A user with that name already exists.")

    user = User(
        username=username,
        password_hash=hash_password(password) if password else None,
        is_owner=is_owner,
    )
    session.add(user)
    if commit:
        await session.commit()
    return user


async def get_user_by_username(session, username: str) -> User | None:
    """Return the user with ``username`` (case-sensitive), or None."""
    return (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()


async def owner_count(session) -> int:
    """Number of owner accounts (must stay 1 after bootstrap)."""
    return (
        await session.execute(
            select(func.count()).select_from(User).where(User.is_owner)
        )
    ).scalar_one()


async def ensure_owner(session) -> User | None:
    """Create the owner account on first run (bootstrap), if auth is enabled.

    Idempotent: when an owner already exists it is returned untouched. A blank
    ``BERU_OWNER_PASSWORD`` yields a random password that is logged (never
    printed), so a fresh install is usable out of the box yet still locked.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return None
    existing = (
        await session.execute(select(User).where(User.is_owner))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    password = settings.owner_password or secrets.token_hex(24)
    user = await create_user(
        session,
        username=settings.owner_username,
        password=password,
        is_owner=True,
        commit=False,
    )
    await session.commit()
    if not settings.owner_password:
        logger.warning(
            "Created owner account '%s' with a randomly generated password "
            "(set BERU_OWNER_PASSWORD to take ownership).",
            user.username,
        )
    return user


async def authenticate_user(session, username: str, password: str) -> User | None:
    """Return the user when ``username``/``password`` match, else None.

    A constant-time verification of the stored PBKDF2 hash. Missing accounts
    and wrong passwords return the same ``None`` so callers cannot distinguish
    which part was invalid.
    """
    user = await get_user_by_username(session, username)
    if user is None or user.password_hash is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user