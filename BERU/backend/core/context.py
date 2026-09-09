"""Per-request authentication context: the current :class:`Principal`.

BERU historically authenticated one shared API key to the whole system. 5.1
adds per-user accounts, so every request needs to know *who* it runs as. The
principal is carried in a context variable — copy-on-task like ``request_id_ctx``
— so downstream services can scope their queries without threading a parameter
through every call site.

The **default principal is the system/owner**: background work (the proactive
scheduler, monitor triggers, migration/adoption, tests that don't exercise
auth) runs unscoped and sees all data, exactly like the legacy single-user
system. Only requests authenticated as a *named* (non-owner) user are scoped to
that user's rows.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """The identity a request (or background task) runs as.

    ``is_owner`` principals and the system default are **unscoped** — they may
    read and write any row. Non-owner principals are scoped to ``user_id``.
    """

    user_id: str | None = None
    username: str | None = None
    is_owner: bool = False
    auth_enabled: bool = False
    mode: str = "system"  # "cookie" | "header" | "none" | "system"


#: System/owner principal used as the default when no request context is set.
SYSTEM_PRINCIPAL = Principal(is_owner=True, auth_enabled=False, mode="system")

_current_principal: ContextVar[Principal] = ContextVar(
    "current_principal", default=SYSTEM_PRINCIPAL
)


def get_current_principal() -> Principal:
    """Return the principal governing the current task.

    Returns :data:`SYSTEM_PRINCIPAL` (owner-like, unscoped) when no request
    has authenticated, so background/system work is never blocked by scoping.
    """
    return _current_principal.get()


def set_current_principal(principal: Principal) -> None:
    """Set the principal for the current task (called by the auth dependencies)."""
    _current_principal.set(principal)


def is_scoped(principal: Principal | None = None) -> bool:
    """True when ``principal`` (or the current one) is user-scoped.

    A principal is scoped when it belongs to a named non-owner account; the
    system/owner default and owner key are never scoped.
    """
    if principal is None:
        principal = get_current_principal()
    return principal.auth_enabled and not principal.is_owner