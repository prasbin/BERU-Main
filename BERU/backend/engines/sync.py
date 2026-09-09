"""Sync engine: device registry, sync cursors, and push notification mailboxes.

A storage-free (in-memory) coordination layer for laptop <-> phone sync. It owns
device registration/revocation, per-device delta cursors, and push-notification
mailboxes. The actual conversation/message deltas against the database are
computed by a sync service (see :mod:`backend.services.sync_service`).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SyncDevice:
    """A registered remote device (e.g. an Android phone)."""

    device_id: str
    name: str
    platform: str = "android"
    capabilities: list[str] = field(default_factory=list)
    push_url: str | None = None
    cursor: int = 0  # microsecond UTC timestamp of the last synced message (0 = never)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "platform": self.platform,
            "capabilities": list(self.capabilities),
            "push_url": self.push_url,
            "cursor": self.cursor,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }


@dataclass
class SyncNotification:
    """A relayed notification queued for a device's push mailbox."""

    id: str
    title: str
    message: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
        }


class SyncEngine:
    """Coordinates registered devices, cursors and push mailboxes."""

    def __init__(self) -> None:
        self._devices: dict[str, SyncDevice] = {}
        self._mailboxes: dict[str, list[SyncNotification]] = {}

    # ---- device registry ----

    def register_device(
        self,
        *,
        name: str,
        platform: str = "android",
        capabilities: list[str] | None = None,
        push_url: str | None = None,
    ) -> SyncDevice:
        device = SyncDevice(
            device_id=uuid.uuid4().hex[:12],
            name=name,
            platform=platform,
            capabilities=list(capabilities or []),
            push_url=push_url,
        )
        self._devices[device.device_id] = device
        self._mailboxes[device.device_id] = []
        logger.info("Sync device registered: %s (%s)", device.device_id, name)
        return device

    def get_device(self, device_id: str) -> SyncDevice | None:
        return self._devices.get(device_id)

    def list_devices(self) -> list[dict]:
        return [d.to_dict() for d in self._devices.values()]

    def revoke_device(self, device_id: str) -> bool:
        """Unregister a device and drop its mailbox. Returns True if removed."""
        if device_id not in self._devices:
            return False
        self._devices.pop(device_id, None)
        self._mailboxes.pop(device_id, None)
        logger.info("Sync device revoked: %s", device_id)
        return True

    def device_count(self) -> int:
        return len(self._devices)

    # ---- cursors / activity ----

    def record_activity(self, device_id: str) -> bool:
        device = self._devices.get(device_id)
        if device is None:
            return False
        device.last_seen = datetime.now(timezone.utc)
        return True

    def set_cursor(self, device_id: str, cursor: int) -> bool:
        device = self._devices.get(device_id)
        if device is None:
            return False
        device.cursor = max(device.cursor, cursor)
        return True

    # ---- push mailbox ----

    def enqueue_notification(
        self, device_id: str, title: str, message: str
    ) -> SyncNotification | None:
        """Queue a notification for a device; None if the device is unknown."""
        if device_id not in self._mailboxes:
            return None
        notification = SyncNotification(
            id=uuid.uuid4().hex[:8],
            title=title,
            message=message,
        )
        self._mailboxes[device_id].append(notification)
        self.record_activity(device_id)
        return notification

    def mailbox(self, device_id: str) -> list[SyncNotification]:
        """Return pending notifications (read does not clear them)."""
        return list(self._mailboxes.get(device_id, []))

    def ack_notifications(self, device_id: str, ids: list[str]) -> int:
        """Acknowledge delivered notifications, removing them; returns count removed."""
        pending = self._mailboxes.get(device_id, [])
        id_set = set(ids)
        before = len(pending)
        self._mailboxes[device_id] = [n for n in pending if n.id not in id_set]
        return before - len(self._mailboxes[device_id])

    # ---- status ----

    def status(self) -> dict:
        return {
            "devices": self.device_count(),
            "platforms": sorted({d.platform for d in self._devices.values()}),
        }