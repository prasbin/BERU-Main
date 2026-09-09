"""Fact ORM model — durable long-term memory of user facts and preferences.

Facts are short, named pieces of information (e.g. "user prefers dark mode",
"name is Alex") that persist across conversations. They can be added, updated,
and forgotten explicitly via the API.

Facts can be scoped to an agent and/or a project for isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.project import Project


class Fact(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "facts"
    __table_args__ = (
        # Fact keys are unique per user (NULL user_id rows — legacy/owner facts
        # in single-user mode — are all distinct under SQLite's NULL handling,
        # so distinct owners can share keys without colliding).
        UniqueConstraint("user_id", "key", name="uq_facts_user_key"),
    )

    # Unique within a user's scope, enforced by the table constraint above.
    key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(
        String(64), default="general", nullable=False, index=True
    )
    # Optional scoping: agent name, project id, or both.
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Owning user (NULL = system/owner rows in the single-user legacy mode).
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    project: Mapped[Project | None] = relationship(back_populates="facts")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Fact key={self.key!r} category={self.category!r}>"
