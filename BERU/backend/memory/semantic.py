"""Semantic recall memory strategy.

Embeds each new user message into a vector store and, at query time, retrieves
the most relevant past messages by cosine similarity. Combined with a short-term
window for recent context, this gives the LLM both recency and relevance.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.engines.embeddings.base import EmbeddingProvider
from backend.engines.llm.base import LLMMessage
from backend.memory.base import BaseMemory
from backend.memory.vector_store import VectorStore
from backend.models.message import Message

logger = logging.getLogger(__name__)

_VALID_ROLES: set[str] = {"system", "user", "assistant"}


class SemanticMemory(BaseMemory):
    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore | None = None,
        *,
        recent_limit: int = 10,
        recall_limit: int = 5,
        similarity_threshold: float = 0.3,
    ) -> None:
        self._embedder = embedding_provider
        self._store = vector_store or VectorStore()
        self._recent_limit = recent_limit
        self._recall_limit = recall_limit
        self._threshold = similarity_threshold

    async def build_context(
        self,
        session: AsyncSession,
        conversation_id: str,
        *,
        limit: int,
    ) -> list[LLMMessage]:
        recent = await self._get_recent(session, conversation_id)
        recalled = await self._get_relevant(session, conversation_id)

        seen_text = {m.content for m in recent}
        context: list[LLMMessage] = []

        if recalled:
            recalled_msgs = [
                m for m in recalled
                if m.content not in seen_text
            ][:self._recall_limit]
            if recalled_msgs:
                context.append(
                    LLMMessage(
                        role="system",
                        content=(
                            "[Relevant past messages]\n"
                            + "\n".join(f"- {m.content}" for m in recalled_msgs)
                        ),
                    )
                )

        context.extend(
            LLMMessage(role=m.role, content=m.content)  # type: ignore[arg-type]
            for m in recent
        )

        if len(context) > limit:
            context = context[-limit:]

        return context

    async def index_message(self, session: AsyncSession, message: Message) -> None:
        if message.role != "user":
            return
        try:
            vectors = await self._embedder.embed([message.content])
            await self._store.upsert(
                session,
                source_table="messages",
                source_id=message.id,
                text=message.content,
                vector=vectors[0],
            )
        except Exception:
            logger.exception("Failed to embed message %s", message.id)

    async def _get_recent(self, session: AsyncSession, conversation_id: str) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(self._recent_limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()
        return rows

    async def _get_relevant(
        self, session: AsyncSession, conversation_id: str
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        latest_user = (await session.execute(stmt)).scalar_one_or_none()
        if latest_user is None:
            return []

        try:
            vectors = await self._embedder.embed([latest_user.content])
        except Exception:
            logger.exception("Failed to embed query")
            return []

        results = await self._store.search(
            session,
            vectors[0],
            source_table="messages",
            top_k=self._recall_limit + 5,
        )

        relevant: list[Message] = []
        for r in results:
            if r.score < self._threshold:
                continue
            if r.source_id == latest_user.id:
                continue
            msg = await session.get(Message, r.source_id)
            if msg is not None and msg.conversation_id == conversation_id:
                relevant.append(msg)

        return relevant
