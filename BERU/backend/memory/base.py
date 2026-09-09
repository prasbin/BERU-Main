"""Memory interface.

A memory implementation turns some source of context into a list of
provider-neutral :class:`LLMMessage` objects that can be prepended to a prompt.
Keeping this behind an interface means richer memory (semantic recall, summaries,
long-term stores) can be added later without changing callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession

from backend.engines.llm.base import LLMMessage


class BaseMemory(ABC):
    @abstractmethod
    async def build_context(
        self, session: AsyncSession, conversation_id: str, *, limit: int
    ) -> list[LLMMessage]:
        """Return prior context for ``conversation_id`` as ordered LLM messages."""
        raise NotImplementedError
