"""Notification ORM model — durable inbox for proactive alerts.

Notifications are created by scheduled tasks, monitor triggers, agents, or the
API. They persist across restarts and are pushed live over the WebSocket
channel when they arrive.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin


class NotificationRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "notifications"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(
        String(16), default="info", nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), default="in_app", nullable=False)
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "level": self.level,
            "channel": self.channel,
            "agent": self.agent,
            "read": self.read,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<NotificationRecord id={self.id!r} level={self.level!r} read={self.read}>"