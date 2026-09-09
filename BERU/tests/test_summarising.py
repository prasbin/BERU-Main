"""Tests for the summarising memory strategy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from backend.engines.llm.base import LLMResponse, LLMUsage
from backend.memory.summarising import SummarisingMemory
from backend.services.conversation_service import ConversationService


def _mock_provider(summary_text: str = "A summary of the conversation.") -> MagicMock:
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content=summary_text,
            model="mock-model",
            usage=LLMUsage(10, 5, 15),
        )
    )
    return provider


async def test_short_conversation_returns_window(db_session):
    """When messages are below the threshold, returns a plain window."""
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    for i in range(5):
        await service.add_message(db_session, conversation, role="user", content=f"msg{i}")
    await db_session.commit()

    provider = _mock_provider()
    memory = SummarisingMemory(provider, keep=3, threshold=10)
    context = await memory.build_context(db_session, conversation.id, limit=10)

    assert [m.content for m in context] == ["msg0", "msg1", "msg2", "msg3", "msg4"]
    # Provider should not have been called (no summarisation needed).
    provider.chat.assert_not_called()


async def test_long_conversation_triggers_summarisation(db_session):
    """When messages exceed the threshold, older messages are summarised."""
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    for i in range(15):
        await service.add_message(db_session, conversation, role="user", content=f"msg{i}")
    await db_session.commit()

    provider = _mock_provider("Summarised context about earlier messages.")
    memory = SummarisingMemory(provider, keep=5, threshold=10)
    context = await memory.build_context(db_session, conversation.id, limit=20)

    # First message should be the system summary.
    assert context[0].role == "system"
    assert "Summarised context" in context[0].content
    assert "[Conversation summary]" in context[0].content

    # Remaining messages should be the 5 most recent (msg10..msg14).
    assert len(context) == 6  # 1 summary + 5 recent
    assert [m.content for m in context[1:]] == ["msg10", "msg11", "msg12", "msg13", "msg14"]

    # Provider should have been called once for summarisation.
    provider.chat.assert_called_once()
    call_args = provider.chat.call_args
    prompt_messages = call_args[0][0]
    assert len(prompt_messages) == 1
    assert "CONVERSATION:" in prompt_messages[0].content


async def test_summarisation_failure_falls_back_to_window(db_session):
    """If the LLM fails to summarise, fall back to a plain window."""
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    for i in range(15):
        await service.add_message(db_session, conversation, role="user", content=f"msg{i}")
    await db_session.commit()

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    memory = SummarisingMemory(provider, keep=5, threshold=10)
    context = await memory.build_context(db_session, conversation.id, limit=20)

    # Should fall back to window: last 15 messages (all of them, limit > count).
    assert len(context) == 15
    assert context[0].role == "user"
    assert context[0].content == "msg0"


async def test_summary_prompt_contains_all_older_messages(db_session):
    """The summarisation prompt should include all older messages."""
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    for i in range(12):
        role = "user" if i % 2 == 0 else "assistant"
        await service.add_message(db_session, conversation, role=role, content=f"turn{i}")
    await db_session.commit()

    provider = _mock_provider("ok")
    memory = SummarisingMemory(provider, keep=3, threshold=8)
    await memory.build_context(db_session, conversation.id, limit=20)

    call_args = provider.chat.call_args
    prompt = call_args[0][0][0].content
    # Older messages (turn0..turn8) should appear in the prompt.
    for i in range(9):
        assert f"turn{i}" in prompt
    # Recent messages (turn9..turn11) should NOT be in the prompt.
    for i in range(9, 12):
        assert f"turn{i}" not in prompt


async def test_limit_respected_with_summary(db_session):
    """The limit parameter should cap the total context length."""
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    for i in range(15):
        await service.add_message(db_session, conversation, role="user", content=f"msg{i}")
    await db_session.commit()

    provider = _mock_provider("summary")
    memory = SummarisingMemory(provider, keep=5, threshold=10)
    context = await memory.build_context(db_session, conversation.id, limit=3)

    # Summary (1) + 2 recent = 3 total.
    assert len(context) == 3
    assert context[0].role == "system"


async def test_empty_conversation(db_session):
    """An empty conversation should return an empty context."""
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    await db_session.commit()

    provider = _mock_provider()
    memory = SummarisingMemory(provider, keep=5, threshold=10)
    context = await memory.build_context(db_session, conversation.id, limit=10)

    assert context == []
    provider.chat.assert_not_called()
