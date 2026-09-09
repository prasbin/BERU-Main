"""Project ORM model — a container for conversations and scoped memory.

Projects provide per-project isolation for conversations, facts, and memory.
Conversations and facts can optionally belong to a project.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.conversation import Conversation
    from backend.models.fact import Fact


class Project(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        # Project names are unique per user (NULL user_id rows — legacy/owner
        # projects in single-user mode — are distinct under SQLite's NULL
        # handling, so different owners can share names without colliding).
        UniqueConstraint("user_id", "name", name="uq_projects_user_name"),
    )

    # Unique within a user's scope, enforced by the table constraint above.
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
