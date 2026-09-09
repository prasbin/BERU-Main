"""Short-term conversation memory: a window over the most recent messages.

This is intentionally simple and deterministic. It loads the last ``limit``
messages for a conversation (chronologically ordered) and maps them to
provider-neutral messages for the prompt.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.engines.llm.base import LLMMessage
from backend.memory.base import BaseMemory
from backend.models.message import Message

_VALID_ROLES: set[str] = {"system", "user", "assistant"}


class ConversationMemory(BaseMemory):
    async def build_context(
        self, session: AsyncSession, conversation_id: str, *, limit: int
    ) -> list[LLMMessage]:
        # Fetch the most recent `limit` messages, then restore chronological order.
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(max(limit, 0))
        )
        rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()
        context: list[LLMMessage] = []
        for msg in rows:
            role = msg.role if msg.role in _VALID_ROLES else "user"
            context.append(LLMMessage(role=role, content=msg.content))  # type: ignore[arg-type]
        return context
