"""User ORM model — a per-person account with its own data scope.

The owner account (``is_owner=True``) is the superuser and the only one that can
manage accounts; ordinary users are isolated to their own rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.session import SessionRecord


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    # PBKDF2 string per backend.core.passwords; no plaintext is ever stored.
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_owner: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    sessions: Mapped[list[SessionRecord]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User id={self.id!r} username={self.username!r} owner={self.is_owner}>"