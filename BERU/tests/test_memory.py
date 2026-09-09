"""Tests for short-term conversation memory (the recent-message window)."""

from __future__ import annotations

from backend.memory.conversation_memory import ConversationMemory
from backend.services.conversation_service import ConversationService


async def test_memory_returns_recent_window_in_order(db_session):
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    for i in range(5):
        await service.add_message(db_session, conversation, role="user", content=f"m{i}")
    await db_session.commit()

    memory = ConversationMemory()
    context = await memory.build_context(db_session, conversation.id, limit=3)

    # The three most recent messages, restored to chronological order.
    assert [m.content for m in context] == ["m2", "m3", "m4"]
    assert all(m.role == "user" for m in context)


async def test_memory_empty_for_new_conversation(db_session):
    service = ConversationService()
    conversation = await service.create(db_session, agent="beru_core")
    await db_session.commit()

    memory = ConversationMemory()
    context = await memory.build_context(db_session, conversation.id, limit=10)
    assert context == []
