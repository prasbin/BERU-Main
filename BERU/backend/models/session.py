"""Session ORM model — the durable, DB-backed store for login sessions.

Only the **SHA-256 hash** of each session token is persisted (never the raw
token), plus the hash of the API-key realm the session was issued under, so
rotating ``BERU_API_KEY`` invalidates every stored session across restarts.
The in-memory session store in :mod:`backend.api.security` is the fast path;
this table is what lets sessions survive a restart (see ``restore_sessions``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.user import User


class SessionRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "sessions"

    # SHA-256 hex digest of the opaque token (the raw token is never stored).
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    # Realm the session is valid under: sha256 of the issuing API key. Rotating
    # the key invalidates sessions whose realm no longer matches.
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    user: Mapped[User | None] = relationship(back_populates="sessions")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<SessionRecord user_id={self.user_id!r} revoked={self.revoked_at is not None}>"