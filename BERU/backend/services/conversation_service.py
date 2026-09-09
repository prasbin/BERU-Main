"""Conversation & message persistence.

Write methods ``flush`` (to populate ids/timestamps) but do not ``commit`` —
the calling service commits once per request so a chat turn is atomic.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.registry import DEFAULT_AGENT_NAME
from backend.core.context import get_current_principal, is_scoped
from backend.core.errors import NotFoundError
from backend.models.conversation import Conversation
from backend.models.message import Message
from backend.models.mixins import utcnow
from backend.services.scoping import owner_scope_user_id, scope_condition


class ConversationService:
    async def create(
        self,
        session: AsyncSession,
        *,
        agent: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> Conversation:
        principal = get_current_principal()
        if project_id is not None:
            # Rejects foreign projects 404-style (also checked in chat_service).
            from backend.services.project_service import ProjectService

            await ProjectService().get(session, project_id)
        conversation = Conversation(
            agent=agent or DEFAULT_AGENT_NAME,
            title=title,
            project_id=project_id,
            user_id=owner_scope_user_id(principal),
        )
        session.add(conversation)
        await session.flush()
        return conversation

    async def get(self, session: AsyncSession, conversation_id: str) -> Conversation:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation '{conversation_id}' not found.")
        principal = get_current_principal()
        if is_scoped(principal) and conversation.user_id != principal.user_id:
            raise NotFoundError(f"Conversation '{conversation_id}' not found.")
        return conversation

    async def add_message(
        self,
        session: AsyncSession,
        conversation: Conversation,
        *,
        role: str,
        content: str,
        model: str | None = None,
        token_count: int | None = None,
    ) -> Message:
        # Keep created_at strictly increasing within a conversation so ordering
        # by created_at is deterministic even when two turns land in the same
        # clock microsecond (SQLite returns rows with equal timestamps in an
        # unspecified order). SQLite round-trips datetimes as naive UTC.
        last = await session.scalar(
            select(func.max(Message.created_at)).where(
                Message.conversation_id == conversation.id
            )
        )
        now = utcnow()
        if last is not None:
            last = last.replace(tzinfo=now.tzinfo)
            if now <= last:
                now = last + timedelta(microseconds=1)
        message = Message(
            conversation_id=conversation.id,
            role=role,
            content=content,
            model=model,
            token_count=token_count,
            created_at=now,
        )
        session.add(message)
        # Reflect activity on the parent conversation.
        conversation.updated_at = utcnow()
        await session.flush()
        return message

    async def list(
        self, session: AsyncSession, *, limit: int = 50, offset: int = 0
    ) -> tuple[int, list[Conversation]]:
        principal = get_current_principal()
        total = (
            await session.scalar(
                select(func.count(Conversation.id)).where(
                    scope_condition(Conversation, principal)
                )
            )
        ) or 0
        stmt = (
            select(Conversation)
            .where(scope_condition(Conversation, principal))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await session.execute(stmt)).scalars().all())
        return total, items

    async def get_messages(self, session: AsyncSession, conversation_id: str) -> list[Message]:
        # Enforces ownership through the scoped get() first.
        await self.get(session, conversation_id)
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        return list((await session.execute(stmt)).scalars().all())
