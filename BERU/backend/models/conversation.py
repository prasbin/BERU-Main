"""Conversation ORM model — a container for an ordered sequence of messages.

Conversations can optionally belong to a project for scoping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.message import Message
    from backend.models.project import Project


class Conversation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Which agent owns this conversation (e.g. "beru_core").
    agent: Mapped[str] = mapped_column(String(64), default="beru_core", nullable=False)
    # Optional project scoping.
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Owning user (NULL = system/owner rows in the single-user legacy mode).
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    project: Mapped[Project | None] = relationship(back_populates="conversations")

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Conversation id={self.id!r} agent={self.agent!r} title={self.title!r}>"
