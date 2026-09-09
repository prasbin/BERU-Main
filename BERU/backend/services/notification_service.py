"""Durable notification inbox (database-backed).

Notifications created by scheduled tasks, monitor triggers, agents, or the API
are persisted here and pushed live to WebSocket clients. Mirrors the read/mark
API of the in-memory engine inbox (``backend/engines/notifications.py``) for a
consistent surface, but survives restarts.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.notification import NotificationRecord

logger = logging.getLogger(__name__)


class NotificationService:
    """Database-backed notification inbox with live WebSocket push."""

    async def create(
        self,
        session: AsyncSession,
        *,
        title: str,
        message: str,
        level: str = "info",
        channel: str = "in_app",
        agent: str | None = None,
        payload: dict | None = None,
    ) -> NotificationRecord:
        """Persist a notification. Caller commits; :meth:`push` broadcasts it."""
        record = NotificationRecord(
            title=title,
            message=message,
            level=level or "info",
            channel=channel or "in_app",
            agent=agent,
            payload=payload or {},
        )
        session.add(record)
        await session.flush()
        return record

    async def push(self, record: NotificationRecord) -> None:
        """Broadcast a persisted notification to connected WebSocket clients.

        A no-op when no clients are connected; never raises.
        """
        from backend.api.routers.websocket import get_ws_manager

        try:
            await get_ws_manager().broadcast(
                {"type": "notification", "notification": record.to_dict()}
            )
        except Exception:  # noqa: BLE001 - a failed push must not lose the write
            logger.exception("Failed to broadcast notification %s", record.id)

    async def get(self, session: AsyncSession, notification_id: str) -> NotificationRecord | None:
        return await session.get(NotificationRecord, notification_id)

    async def list(
        self,
        session: AsyncSession,
        *,
        unread_only: bool = False,
        agent: str | None = None,
        limit: int = 50,
    ) -> list[NotificationRecord]:
        stmt = select(NotificationRecord)
        if unread_only:
            stmt = stmt.where(NotificationRecord.read.is_(False))
        if agent:
            stmt = stmt.where(NotificationRecord.agent == agent)
        stmt = stmt.order_by(NotificationRecord.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def unread_count(self, session: AsyncSession) -> int:
        stmt = select(func.count(NotificationRecord.id)).where(
            NotificationRecord.read.is_(False)
        )
        return int((await session.execute(stmt)).scalar_one())

    async def mark_read(self, session: AsyncSession, notification_id: str) -> bool:
        result = await session.execute(
            update(NotificationRecord)
            .where(
                NotificationRecord.id == notification_id,
                NotificationRecord.read.is_(False),
            )
            .values(read=True)
        )
        return result.rowcount > 0

    async def mark_all_read(self, session: AsyncSession) -> int:
        result = await session.execute(
            update(NotificationRecord).values(read=True)
        )
        return result.rowcount or 0

    async def delete(self, session: AsyncSession, notification_id: str) -> bool:
        result = await session.execute(
            delete(NotificationRecord).where(NotificationRecord.id == notification_id)
        )
        return result.rowcount > 0

    async def clear(self, session: AsyncSession) -> int:
        result = await session.execute(delete(NotificationRecord))
        return result.rowcount or 0