"""Sync service: database-backed deltas for remote (phone) devices.

Computes conversation/message deltas since a device's cursor and accepts
messages authored on the device. Cursors are microsecond UTC timestamps of the
last synced message; the engine records them per device.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.errors import NotFoundError
from backend.engines.sync import SyncDevice, SyncEngine
from backend.models.conversation import Conversation
from backend.models.message import Message
from backend.models.mixins import utcnow

_EPOCH = datetime(1970, 1, 1)


def _to_micros(dt: datetime) -> int:
    """Convert a datetime to exact UTC-microsecond-since-epoch.

    SQLite stores ``DateTime(timezone=True)`` as a naive UTC string, so naive
    input is treated as UTC and tz-aware input is normalised to UTC first.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return calendar.timegm(dt.utctimetuple()) * 1_000_000 + dt.microsecond


def _from_micros(micros: int) -> datetime:
    """Naive-UTC datetime from a cursor (matches SQLite's stripped-tz storage)."""
    return _EPOCH + timedelta(microseconds=micros)


def message_to_dict(message: Message) -> dict:
    """Serialize a message for the phone payload."""
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }


def conversation_to_dict(conversation: Conversation) -> dict:
    """Serialize a conversation for the phone payload."""
    return {
        "id": conversation.id,
        "title": conversation.title,
        "agent": conversation.agent,
        "updated_at": conversation.updated_at.isoformat(),
    }


class SyncService:
    """Provides pull (delta) and push (phone-authored message) operations."""

    def __init__(self, engine: SyncEngine | None = None) -> None:
        self._engine = engine or SyncEngine()

    @property
    def engine(self) -> SyncEngine:
        return self._engine

    def _require_device(self, device_id: str) -> SyncDevice:
        device = self._engine.get_device(device_id)
        if device is None:
            raise NotFoundError(f"Unknown sync device '{device_id}'.")
        return device

    async def pull(
        self,
        session: AsyncSession,
        device_id: str,
        *,
        limit: int = 50,
    ) -> dict:
        """Return messages/conversations created after the device cursor.

        The device cursor advances to the newest returned message. Messages are
        ordered newest-first so the newest appear in the limit first.
        """
        device = self._require_device(device_id)
        since = _from_micros(device.cursor) if device.cursor else _EPOCH

        stmt = (
            select(Message)
            .where(Message.created_at > since)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        messages = list((await session.execute(stmt)).scalars().all())

        conversations_by_id: dict[str, Conversation] = {}
        if messages:
            conversation_ids = {m.conversation_id for m in messages}
            conv_stmt = select(Conversation).where(Conversation.id.in_(conversation_ids))
            conversations_by_id = {
                c.id: c
                for c in (await session.execute(conv_stmt)).scalars().all()
            }
            newest_cursor = max(_to_micros(m.created_at) for m in messages)
            self._engine.set_cursor(device_id, newest_cursor)

        self._engine.record_activity(device_id)
        return {
            "device_id": device.device_id,
            "cursor": device.cursor,
            "messages": [message_to_dict(m) for m in sorted(messages, key=lambda m: m.created_at)],
            "conversations": [
                conversation_to_dict(conversations_by_id[cid])
                for cid in sorted(
                    {m.conversation_id for m in messages},
                    key=lambda cid0: cid0,
                )
            ],
        }

    async def push_message(
        self,
        session: AsyncSession,
        device_id: str,
        *,
        conversation_id: str,
        content: str,
    ) -> dict:
        """Persist a user message authored on the device."""
        self._require_device(device_id)
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation '{conversation_id}' not found.")

        message = Message(
            conversation_id=conversation.id,
            role="user",
            content=content,
        )
        session.add(message)
        conversation.updated_at = utcnow()
        await session.flush()
        await session.commit()

        record = message_to_dict(message)
        # NOTE: pushing does NOT advance the device cursor. The next pull delivers
        # the phone-authored message back to it, which the client deduplicates by
        # message id; this keeps pull as the single source of ordering truth.
        self._engine.record_activity(device_id)
        return record