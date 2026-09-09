"""Chat orchestration service.

Ties together conversation persistence, short-term memory, and the intelligence
engine to process a single chat turn atomically:

    resolve/create conversation
        -> assemble prior context (memory window + long-term facts)
        -> persist the user message
        -> generate a reply via the engine
        -> persist the assistant message
        -> commit
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import AgentResult, PendingConfirmation
from backend.core.config import Settings
from backend.core.logging import get_logger, request_id_ctx
from backend.database.base import get_sessionmaker
from backend.engines.intelligence import IntelligenceEngine
from backend.engines.llm.base import LLMMessage, LLMUsage, ToolCall
from backend.memory.base import BaseMemory
from backend.memory.semantic import SemanticMemory
from backend.models.conversation import Conversation
from backend.models.message import Message
from backend.schemas.chat import ChatRequest
from backend.services.activity_ledger import (
    AuditEntry,
    error_summary,
    get_activity_ledger,
)
from backend.services.confirmation_service import (
    ConfirmationRecord,
    ConfirmationService,
    get_confirmation_service,
)
from backend.services.conversation_service import ConversationService
from backend.services.fact_service import FactService
from backend.tools.base import serialize_tool_result

logger = get_logger(__name__)


@dataclass
class ChatOutcome:
    conversation: Conversation
    assistant_message: Message
    result: AgentResult
    pending_confirmations: list[dict] | None = None


@dataclass
class ChatStreamEvent:
    """A single event emitted while streaming a chat turn.

    ``kind`` discriminates the payload:
      * ``start`` — first event; carries ``conversation_id`` and ``agent``.
      * ``delta`` — an incremental piece of the reply in ``text``.
      * ``end``   — final event; carries the persisted ``message_id`` plus
        ``model``/``usage`` and any pending tool confirmations.
      * ``error`` — generation failed after the stream began; carries ``error``.
    """

    kind: Literal["start", "delta", "end", "error"]
    conversation_id: str | None = None
    agent: str | None = None
    text: str | None = None
    message_id: str | None = None
    model: str | None = None
    usage: LLMUsage | None = None
    error: str | None = None
    pending_confirmations: list[dict] | None = None


def _default_stream_session_factory():
    """Default factory resolving the app-wide async session maker."""
    return get_sessionmaker()


#: Provider used by :meth:`ChatService.stream_process` to open short-lived
#: sessions per stage. Swappable so tests can pin streaming to their temp DB
#: (mirrors ``set_session_factory`` in ``proactive_service``).
_stream_session_factory = _default_stream_session_factory


def set_stream_session_factory(factory) -> None:
    """Swap the process DB factory used by streaming chat stages (tests)."""
    global _stream_session_factory
    _stream_session_factory = factory


def reset_stream_session_factory() -> None:
    """Restore the default DB factory for streaming chat stages."""
    global _stream_session_factory
    _stream_session_factory = _default_stream_session_factory


class ChatService:
    def __init__(
        self,
        engine: IntelligenceEngine,
        conversation_service: ConversationService,
        memory: BaseMemory,
        fact_service: FactService | None = None,
        confirmations: ConfirmationService | None = None,
    ) -> None:
        self._engine = engine
        self._conversations = conversation_service
        self._memory = memory
        self._facts = fact_service or FactService()
        self._semantic = memory if isinstance(memory, SemanticMemory) else None
        self._confirmations = confirmations or get_confirmation_service()

    def _store_confirmations(
        self,
        *,
        conversation_id: str,
        agent: str,
        pending: list[PendingConfirmation],
    ) -> list[dict]:
        """Persist pending tool calls and return their serialized records."""
        if not pending:
            return []
        stored = []
        for item in pending:
            record = self._confirmations.create(
                conversation_id=conversation_id,
                agent=agent,
                tool_name=item.tool_name,
                arguments=item.arguments,
                tool_call_id=item.tool_call_id,
            )
            stored.append(record.to_dict())
        return stored

    async def _build_context(
        self,
        session: AsyncSession,
        conversation_id: str,
        settings: Settings,
        *,
        agent: str | None = None,
        project_id: str | None = None,
    ) -> list[LLMMessage]:
        """Build the full context: long-term facts + conversation history."""
        context: list[LLMMessage] = []

        # Prepend long-term facts as a system message if any exist.
        fact_texts = await self._facts.all_as_text(
            session,
            agent=agent,
            project_id=project_id,
            limit=50,
        )
        if fact_texts:
            facts_block = "\n".join(f"- {t}" for t in fact_texts)
            context.append(
                LLMMessage(
                    role="system",
                    content=(
                        "[Long-term memory — user facts and preferences]\n"
                        f"{facts_block}"
                    ),
                )
            )

        # Append conversation history from the memory strategy.
        history = await self._memory.build_context(
            session, conversation_id, limit=settings.memory_window_size
        )
        context.extend(history)
        return context

    async def _maybe_index(self, session: AsyncSession, message: Message) -> None:
        """Index a user message for semantic recall if using SemanticMemory."""
        if self._semantic is not None:
            await self._semantic.index_message(session, message)

    async def process(
        self, session: AsyncSession, settings: Settings, request: ChatRequest
    ) -> ChatOutcome:
        # 1. Resolve or create the conversation.
        if request.conversation_id:
            conversation = await self._conversations.get(session, request.conversation_id)
        else:
            conversation = await self._conversations.create(
                session, agent=request.agent, title=request.title,
                project_id=request.project_id,
            )

        # Agent for this turn: explicit override, else the conversation's agent.
        agent_name = request.agent or conversation.agent

        # 2. Assemble prior context BEFORE persisting the new user message.
        history = await self._build_context(
            session,
            conversation.id,
            settings,
            agent=agent_name,
            project_id=conversation.project_id,
        )

        # 3. Persist the user message.
        user_message = await self._conversations.add_message(
            session, conversation, role="user", content=request.message
        )

        # 3b. Index for semantic recall (no-op if not using SemanticMemory).
        await self._maybe_index(session, user_message)

        # 4. Generate the reply.
        result = await self._engine.generate(
            agent_name=agent_name,
            history=history,
            user_message=request.message,
            settings=settings,
        )

        # 5. Persist the assistant message.
        completion_tokens = result.usage.completion_tokens if result.usage else None
        assistant_message = await self._conversations.add_message(
            session,
            conversation,
            role="assistant",
            content=result.content,
            model=result.model,
            token_count=completion_tokens,
        )

        # 6. Commit the whole turn atomically.
        await session.commit()

        return ChatOutcome(
            conversation=conversation,
            assistant_message=assistant_message,
            result=result,
            pending_confirmations=self._store_confirmations(
                conversation_id=conversation.id,
                agent=result.agent,
                pending=result.pending_confirmations,
            ),
        )

    async def stream_process(
        self, settings: Settings, request: ChatRequest
    ) -> AsyncIterator[ChatStreamEvent]:
        """Process a chat turn while streaming the reply.

        Yields a ``start`` event, then ``delta`` events as tokens arrive, then a
        final ``end`` event once the assistant message is persisted. The
        conversation and user message are committed before the stream begins,
        and the assistant message is committed separately when the stream
        completes.

        The caller's DB session is intentionally NOT held across the stream:
        each stage opens its own short-lived session (commit-and-close before
        the first yield, and again for the assistant reply). LLM streaming
        therefore never pins a database connection, so a long turn cannot block
        concurrent writers waiting on the pool.

        Conversation/agent resolution happens before the first yield, so an
        unknown conversation or agent surfaces as an exception to the caller
        (mapped to a 404) *before* the SSE response begins. If generation fails
        mid-stream, an ``error`` event is emitted and no assistant message is
        persisted, but the user's message is retained.
        """
        session_factory = _stream_session_factory()

        # Phase 1 — commit the user turn, then release the session before the
        # LLM stream starts (bad conversation id or agent -> exception, pre-yield).
        async with session_factory() as session:
            if request.conversation_id:
                conversation = await self._conversations.get(session, request.conversation_id)
            else:
                conversation = await self._conversations.create(
                    session, agent=request.agent, title=request.title,
                    project_id=request.project_id,
                )

            agent_name = request.agent or conversation.agent
            # Validate the agent up front (unknown -> NotFoundError, before start).
            self._engine.resolve_agent(agent_name)

            # Assemble prior context BEFORE persisting the new user message.
            history = await self._build_context(
                session,
                conversation.id,
                settings,
                agent=agent_name,
                project_id=conversation.project_id,
            )
            user_msg = await self._conversations.add_message(
                session, conversation, role="user", content=request.message
            )

            # Index for semantic recall (no-op if not using SemanticMemory).
            await self._maybe_index(session, user_msg)

            # Persist the conversation + user message up front so the request is
            # never lost, even if the stream is interrupted mid-delivery.
            await session.commit()
            conversation_id = conversation.id

        yield ChatStreamEvent(
            kind="start", conversation_id=conversation_id, agent=agent_name
        )

        parts: list[str] = []
        model: str | None = None
        usage: LLMUsage | None = None
        resolved_agent = agent_name
        pending: list[PendingConfirmation] = []
        try:
            # Phase 2 — stream tokens with NO database session held: a long
            # provider call must not pin a connection from the pool.
            async for chunk in self._engine.stream_generate(
                agent_name=agent_name,
                history=history,
                user_message=request.message,
                settings=settings,
            ):
                if chunk.done:
                    model = chunk.model
                    usage = chunk.usage
                    resolved_agent = chunk.agent or agent_name
                    pending = list(chunk.pending_confirmations)
                elif chunk.delta:
                    parts.append(chunk.delta)
                    yield ChatStreamEvent(kind="delta", text=chunk.delta)

            content = "".join(parts)
            completion_tokens = usage.completion_tokens if usage else None

            # Phase 3 — persist the assistant reply in a fresh short-lived
            # session once generation has finished.
            async with session_factory() as session:
                conversation = await self._conversations.get(
                    session, conversation_id
                )
                assistant_message = await self._conversations.add_message(
                    session,
                    conversation,
                    role="assistant",
                    content=content,
                    model=model,
                    token_count=completion_tokens,
                )
                await session.commit()
                assistant_message_id = assistant_message.id
        except Exception:  # noqa: BLE001 - surface as a stream event, never crash the SSE
            logger.exception("Streaming chat generation failed")
            # Generic message: do not leak provider/internal details to clients.
            yield ChatStreamEvent(kind="error", error="Generation failed.")
            return

        confirmations = self._store_confirmations(
            conversation_id=conversation_id,
            agent=resolved_agent,
            pending=pending,
        )
        yield ChatStreamEvent(
            kind="end",
            conversation_id=conversation_id,
            agent=resolved_agent,
            message_id=assistant_message_id,
            model=model,
            usage=usage,
            pending_confirmations=confirmations,
        )

    async def confirm_tool_call(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        conversation_id: str,
        confirmation_id: str,
    ) -> ChatOutcome:
        """Execute a pending tool call and let the agent conclude the turn.

        The confirmation record is consumed (single-use) and the recorded tool
        call is re-invoked with ``confirm=True`` — the tool's own argument JSON
        is reused, so the stored intent is what actually runs. The agent then
        produces its final reply grounded in the tool result, which is persisted
        as a normal assistant message.
        """
        record = self._confirmations.consume(conversation_id, confirmation_id)

        conversation = await self._conversations.get(session, conversation_id)
        agent_name = record.agent or conversation.agent
        self._engine.resolve_agent(agent_name)

        # Recover the original user request that prompted the tool call.
        original = ""
        for item in reversed(await self._conversations.get_messages(session, conversation.id)):
            if item.role == "user":
                original = item.content
                break

        history = await self._build_context(
            session,
            conversation.id,
            settings,
            agent=agent_name,
            project_id=conversation.project_id,
        )

        tool_call = ToolCall(
            id=record.tool_call_id,
            name=record.tool_name,
            arguments=json.dumps(record.arguments),
        )
        result = await self._engine.execute_tool_call(
            agent_name=agent_name,
            tool_call=tool_call,
            confirm=True,
        )
        # Record approval audit entry.
        try:
            get_activity_ledger().record_audit(AuditEntry(
                timestamp=time.time(),
                request_id=request_id_ctx.get(),
                agent=agent_name,
                tool_name=record.tool_name,
                decision="approved",
                confirmation_id=confirmation_id,
                outcome="success" if result.ok else "failure",
                error=error_summary(result.error),
            ))
        except Exception:
            logger.debug("Could not record audit entry", exc_info=True)
        tool_result_text = serialize_tool_result(result, tool_name=tool_call.name)
        final = await self._engine.continue_after_tool(
            agent_name=agent_name,
            history=history,
            user_message=original,
            tool_call=tool_call,
            tool_result_text=tool_result_text,
            settings=settings,
        )

        completion_tokens = final.usage.completion_tokens if final.usage else None
        assistant_message = await self._conversations.add_message(
            session,
            conversation,
            role="assistant",
            content=final.content,
            model=final.model,
            token_count=completion_tokens,
        )
        await session.commit()

        return ChatOutcome(
            conversation=conversation,
            assistant_message=assistant_message,
            result=final,
        )

    def deny_tool_call(self, conversation_id: str, confirmation_id: str) -> ConfirmationRecord:
        """Deny a pending tool call without executing it.

        The record is consumed exactly like an approval (single-use), so the
        tool can never run afterwards — not by replaying the id, not by an
        attacker in a later request.
        """
        record = self._confirmations.consume(conversation_id, confirmation_id)
        # Record denial audit entry.
        try:
            get_activity_ledger().record_audit(AuditEntry(
                timestamp=time.time(),
                request_id=request_id_ctx.get(),
                agent=record.agent or "",
                tool_name=record.tool_name,
                decision="denied",
                confirmation_id=confirmation_id,
                outcome=None,
                error=None,
            ))
        except Exception:
            logger.debug("Could not record audit entry", exc_info=True)
        return record

    def list_pending_confirmations(self, conversation_id: str) -> list[dict]:
        """Return unresolved tool confirmations for a conversation (for a UI)."""
        return [r.to_dict() for r in self._confirmations.list_for(conversation_id)]
