"""Notification system — manages alerts, reminders, and proactive messages.

Supports different notification channels (in-app, webhook, future: email/SMS)
and stores notification history with read/unread status.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class NotificationLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


class NotificationChannel(str, Enum):
    IN_APP = "in_app"
    WEBHOOK = "webhook"
    EMAIL = "email"
    SMS = "sms"


@dataclass
class Notification:
    """A single notification."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    message: str = ""
    level: NotificationLevel = NotificationLevel.INFO
    channel: NotificationChannel = NotificationChannel.IN_APP
    agent: str | None = None
    read: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "level": self.level.value,
            "channel": self.channel.value,
            "agent": self.agent,
            "read": self.read,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Notification:
        created_at = data.get("created_at")
        if created_at and isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            title=data.get("title", ""),
            message=data.get("message", ""),
            level=NotificationLevel(data.get("level", "info")),
            channel=NotificationChannel(data.get("channel", "in_app")),
            agent=data.get("agent"),
            read=data.get("read", False),
            created_at=created_at or datetime.now(timezone.utc),
            metadata=data.get("metadata", {}),
        )


class NotificationService:
    """Manages notifications — creation, retrieval, and delivery."""

    def __init__(self) -> None:
        self._notifications: dict[str, Notification] = {}
        self._webhook_urls: list[str] = []

    def add_webhook(self, url: str) -> None:
        """Register a webhook URL for notification delivery."""
        if url not in self._webhook_urls:
            self._webhook_urls.append(url)

    def remove_webhook(self, url: str) -> bool:
        """Remove a webhook URL."""
        if url in self._webhook_urls:
            self._webhook_urls.remove(url)
            return True
        return False

    def notify(
        self,
        title: str,
        message: str,
        level: NotificationLevel = NotificationLevel.INFO,
        channel: NotificationChannel = NotificationChannel.IN_APP,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Notification:
        """Create and store a notification."""
        notif = Notification(
            title=title,
            message=message,
            level=level,
            channel=channel,
            agent=agent,
            metadata=metadata or {},
        )
        self._notifications[notif.id] = notif
        logger.info("Notification: [%s] %s", level.value, title)
        return notif

    def get(self, notification_id: str) -> Notification | None:
        return self._notifications.get(notification_id)

    def list(
        self,
        unread_only: bool = False,
        agent: str | None = None,
        limit: int = 50,
    ) -> list[Notification]:
        notifs = list(self._notifications.values())

        if unread_only:
            notifs = [n for n in notifs if not n.read]
        if agent:
            notifs = [n for n in notifs if n.agent == agent]

        # Sort by creation time, newest first
        notifs.sort(key=lambda n: n.created_at, reverse=True)
        return notifs[:limit]

    def mark_read(self, notification_id: str) -> bool:
        notif = self._notifications.get(notification_id)
        if notif:
            notif.read = True
            return True
        return False

    def mark_all_read(self) -> int:
        count = 0
        for notif in self._notifications.values():
            if not notif.read:
                notif.read = True
                count += 1
        return count

    def delete(self, notification_id: str) -> bool:
        if notification_id in self._notifications:
            del self._notifications[notification_id]
            return True
        return False

    def clear(self) -> int:
        count = len(self._notifications)
        self._notifications.clear()
        return count

    @property
    def unread_count(self) -> int:
        return sum(1 for n in self._notifications.values() if not n.read)
