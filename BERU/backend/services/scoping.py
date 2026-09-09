"""Multi-user scoping helpers (Stage 5.1).

Services filter their queries by the current :class:`Principal`:

.. code-block:: python

    stmt = stmt.where(scope_condition(Conversation, principal))

    owner_user_id = owner_scope_user_id(principal)

A non-owner principal sees **only its own rows** (``user_id == principal.user_id``);
an owner/system principal is unscoped and sees everything, including legacy NULL
rows. The comparison deliberately treats NULL rows as visible to no scoped user,
so legacy single-user data stays private to the owner after enabling accounts.
"""

from __future__ import annotations

from backend.core.context import Principal, is_scoped


def scope_condition(model_type, principal: Principal):
    """Return a SQLAlchemy WHERE condition restricting queries to ``principal``.

    Unscoped (owner/system) principals get an always-true condition.
    """
    if not is_scoped(principal):
        import sqlalchemy as sa

        return sa.true()
    return model_type.user_id == principal.user_id


def owner_scope_user_id(principal: Principal) -> str | None:
    """The ``user_id`` value new rows should be stamped with for ``principal``.

    None for owner/system principals — those rows remain unscoped legacy rows
    (owned by the system), visible to any owner.
    """
    return principal.user_id if is_scoped(principal) else None