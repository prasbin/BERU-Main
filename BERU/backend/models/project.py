"""Project ORM model — a container for conversations and scoped memory.

Projects provide per-project isolation for conversations, facts, and memory.
Conversations and facts can optionally belong to a project.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.conversation import Conversation
    from backend.models.fact import Fact


class Project(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        # Project names are unique per user (scoped rows).
        UniqueConstraint("user_id", "name", name="uq_projects_user_name"),
        # SQLite and Postgres treat NULLs as distinct in UNIQUE constraints, so
        # system/owner rows (user_id IS NULL) would otherwise lose the legacy
        # global name uniqueness. A partial unique index preserves it.
        Index(
            "uq_projects_system_name",
            "name",
            unique=True,
            sqlite_where=text("user_id IS NULL"),
            postgresql_where=text("user_id IS NULL"),
        ),
    )

    # Unique within a user's scope (or globally for system rows).
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Owning user (NULL = system/owner rows in the single-user legacy mode).
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    facts: Mapped[list[Fact]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Project id={self.id!r} name={self.name!r}>"
