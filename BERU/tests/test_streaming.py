"""Tests for streaming: the mock provider's stream, and the SSE chat endpoint."""

from __future__ import annotations

import json

from backend.engines.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMUsage
from backend.engines.llm.mock import MockProvider


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE response body into a list of (event_name, data) tuples."""
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        data = "{}"
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        events.append((name, json.loads(data)))
    return events


# ---------------------------------------------------------------- provider ----


async def test_mock_stream_chunks_reconstruct_full_reply():
    provider = MockProvider()
    messages = [LLMMessage(role="user", content="hello world")]

    deltas: list[str] = []
    done = None
    async for chunk in provider.stream_chat(messages):
        if chunk.done:
            done = chunk
        else:
            deltas.append(chunk.delta)

    streamed = "".join(deltas)
    buffered = (await provider.chat(messages)).content
    # Streaming must reproduce the buffered reply exactly, and echo the input.
    assert streamed == buffered
    assert "hello world" in streamed

    # Terminal chunk carries model + usage.
    assert done is not None
    assert done.model
    assert done.usage is not None
    assert done.usage.total_tokens > 0


async def test_base_stream_falls_back_to_chat():
    """A provider that only implements chat() still streams via the default."""

    class OnlyChatProvider(LLMProvider):
        name = "only_chat"

        async def chat(self, messages, **kwargs) -> LLMResponse:
            return LLMResponse(
                content="one two three",
                model="fallback-model",
                usage=LLMUsage(1, 3, 4),
                finish_reason="stop",
            )

    provider = OnlyChatProvider()
    deltas, done = [], None
    async for chunk in provider.stream_chat([LLMMessage(role="user", content="hi")]):
        (deltas.append(chunk.delta) if not chunk.done else None)
        if chunk.done:
            done = chunk

    assert "".join(deltas) == "one two three"
    assert done is not None and done.model == "fallback-model"
    assert done.usage is not None and done.usage.total_tokens == 4


# ---------------------------------------------------------------- endpoint ----


async def test_stream_endpoint_streams_and_persists(client):
    resp = await client.post("/api/v1/chat/stream", json={"message": "Stream hello"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    kinds = [name for name, _ in events]
    assert kinds[0] == "start"
    assert kinds[-1] == "end"
    assert "delta" in kinds

    start = events[0][1]
    end = events[-1][1]
    conversation_id = start["conversation_id"]
    assert conversation_id
    assert start["agent"] == "beru_core"
    assert end["conversation_id"] == conversation_id
    assert end["message_id"]
    assert end["usage"]["total_tokens"] >= 0

    # Concatenated deltas equal the persisted assistant message.
    streamed = "".join(d["text"] for name, d in events if name == "delta")
    assert "Stream hello" in streamed

    detail = await client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Stream hello"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == streamed
    assert messages[1]["id"] == end["message_id"]


async def test_stream_endpoint_continues_conversation(client):
    first = await client.post("/api/v1/chat/stream", json={"message": "First"})
    conversation_id = _parse_sse(first.text)[0][1]["conversation_id"]

    second = await client.post(
        "/api/v1/chat/stream",
        json={"message": "Second", "conversation_id": conversation_id},
    )
    assert second.status_code == 200
    assert _parse_sse(second.text)[0][1]["conversation_id"] == conversation_id

    messages = (await client.get(f"/api/v1/conversations/{conversation_id}/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]


async def test_stream_endpoint_rejects_empty_message(client):
    resp = await client.post("/api/v1/chat/stream", json={"message": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "validation_error"


async def test_stream_endpoint_unknown_agent_returns_404(client):
    resp = await client.post("/api/v1/chat/stream", json={"message": "hi", "agent": "ghost"})
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


async def test_stream_endpoint_missing_conversation_returns_404(client):
    resp = await client.post(
        "/api/v1/chat/stream",
        json={"message": "hi", "conversation_id": "does-not-exist"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


async def test_stream_releases_session_before_generation(_sessionmaker):
    """MED#6: the stream persists the user turn and then holds no session.

    After the first (start) event is produced, the conversation + user message
    must already be durable from a brand-new session — proof the stage-1 session
    was committed and closed before LLM token streaming began. Abandoning the
    stream must not leave an assistant message behind.
    """
    from backend.api.deps import (
        get_conversation_service,
        get_intelligence_engine,
        get_memory,
    )
    from backend.services.chat_service import (
        ChatService,
        reset_stream_session_factory,
        set_stream_session_factory,
    )

    set_stream_session_factory(lambda: _sessionmaker)
    try:
        service = ChatService(
            get_intelligence_engine(), get_conversation_service(), get_memory()
        )
        from backend.core.config import get_settings
        from backend.schemas.chat import ChatRequest

        stream = service.stream_process(
            get_settings(), ChatRequest(message="hold-no-session")
        )
        first = await stream.__anext__()
        assert first.kind == "start"
        assert first.conversation_id

        # Stage-1 session is committed and closed: a fresh session sees the
        # user message without contention (pool slot is free).
        conversations = get_conversation_service()
        async with _sessionmaker() as probe:
            conversation = await conversations.get(probe, first.conversation_id)
            messages = await conversations.get_messages(probe, conversation.id)
            assert [m.role for m in messages] == ["user"]
            assert messages[0].content == "hold-no-session"

        # Abandon the generation mid-stream: no assistant reply may appear.
        await stream.aclose()
        async with _sessionmaker() as probe:
            conversation = await conversations.get(probe, first.conversation_id)
            messages = await conversations.get_messages(probe, conversation.id)
            assert [m.role for m in messages] == ["user"]
    finally:
        reset_stream_session_factory()
