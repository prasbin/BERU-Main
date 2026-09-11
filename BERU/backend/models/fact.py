"""Fact ORM model — durable long-term memory of user facts and preferences.

Facts are short, named pieces of information (e.g. "user prefers dark mode",
"name is Alex") that persist across conversations. They can be added, updated,
and forgotten explicitly via the API.

Facts can be scoped to an agent and/or a project for isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.project import Project


class Fact(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "facts"
    __table_args__ = (
        # Fact keys are unique per user (scoped rows).
        UniqueConstraint("user_id", "key", name="uq_facts_user_key"),
        # SQLite and Postgres treat NULLs as distinct in UNIQUE constraints, so
        # system/owner rows (user_id IS NULL) would otherwise lose the legacy
        # global key uniqueness. A partial unique index preserves it exactly
        # for that class of rows on both databases.
        Index(
            "uq_facts_system_key",
            "key",
            unique=True,
            sqlite_where=text("user_id IS NULL"),
            postgresql_where=text("user_id IS NULL"),
        ),
    )

    # Unique within a user's scope (or globally for system rows), enforced by
    # the constraints above.
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
