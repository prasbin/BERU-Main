"""Summarising memory strategy.

When a conversation exceeds a message-count threshold, older messages are
compressed into a summary via the configured LLM provider. The most recent
``keep`` messages are always kept verbatim. This fits behind the same
:class:`BaseMemory` interface so callers don't change.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.engines.llm.base import LLMMessage, LLMProvider
from backend.memory.base import BaseMemory
from backend.models.message import Message

logger = get_logger(__name__)

_VALID_ROLES: set[str] = {"system", "user", "assistant"}

_SUMMARISE_PROMPT = (
    "You are a helpful assistant. Summarise the following conversation "
    "into a concise paragraph that captures the key facts, decisions, and "
    "context. Keep it under 200 words.\n\n"
    "CONVERSATION:\n{conversation}\n\n"
    "SUMMARY:"
)


class SummarisingMemory(BaseMemory):
    """Memory strategy that summarises older messages when history is long.

    Args:
        provider: The LLM provider used to generate summaries.
        keep: Number of recent messages kept verbatim (never summarised).
        threshold: Total message count above which summarisation is triggered.
            Must be greater than *keep*.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        keep: int = 10,
        threshold: int = 20,
    ) -> None:
        self._provider = provider
        self._keep = keep
        self._threshold = max(threshold, keep + 1)

    async def build_context(
        self,
        session: AsyncSession,
        conversation_id: str,
        *,
        limit: int,
    ) -> list[LLMMessage]:
        # Fetch all messages in the conversation (up to a generous cap).
        cap = max(limit, self._threshold) + 50
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(cap)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()

        # If short enough, just return the window (no summarisation).
        if len(rows) <= self._threshold:
            trimmed = rows[-limit:] if limit < len(rows) else rows
            return [self._to_llm(m) for m in trimmed]

        # Split: older messages to summarise, recent messages kept verbatim.
        older = rows[: -self._keep]
        recent = rows[-self._keep :]

        # Build the prompt for summarisation.
        conversation_text = "\n".join(f"{m.role.upper()}: {m.content}" for m in older)
        prompt = _SUMMARISE_PROMPT.format(conversation=conversation_text)

        try:
            response = await self._provider.chat(
                [LLMMessage(role="user", content=prompt)],
            )
            summary = response.content.strip()
        except Exception:
            logger.exception("Summarisation failed; falling back to window")
            # Fallback: just return the most recent messages without summary.
            trimmed = rows[-limit:] if limit < len(rows) else rows
            return [self._to_llm(m) for m in trimmed]

        # Assemble: system summary + recent messages (trimmed to limit).
        context: list[LLMMessage] = [
            LLMMessage(role="system", content=f"[Conversation summary]\n{summary}"),
        ]
        for msg in recent:
            context.append(self._to_llm(msg))

        # Trim to the requested limit (summary counts as 1).
        if len(context) > limit:
            context = [context[0]] + context[-(limit - 1) :]

        return context

    @staticmethod
    def _to_llm(msg: Message) -> LLMMessage:
        role = msg.role if msg.role in _VALID_ROLES else "user"
        return LLMMessage(role=role, content=msg.content)  # type: ignore[arg-type]
