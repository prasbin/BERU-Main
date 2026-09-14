"""Tests for the full voice conversation chain (STT -> chat/agent -> LLM -> TTS).

The chain is hermetic here: deterministic mock STT, mock LLM (which still runs
the real agent tool loop, including a genuine ``clock`` tool call), and mock
TTS. The same service is exercised at both the service level (with a temp
SQLite conversation store) and through the HTTP API. Honest-failure behaviour
(no fabricated transcript, no fabricated reply) is asserted explicitly.
"""

from __future__ import annotations

import base64

import pytest

from backend.agents.registry import get_agent_registry
from backend.core.config import get_settings
from backend.core.errors import BadRequestError
from backend.engines.intelligence import IntelligenceEngine
from backend.engines.llm.mock import MockProvider
from backend.engines.speech import MockSTTProvider, MockTTSProvider
from backend.engines.voice import VoiceEngine
from backend.memory.conversation_memory import ConversationMemory
from backend.services.chat_service import ChatService
from backend.services.conversation_service import ConversationService
from backend.services.voice_chat import VoiceChatService


def _chain(stt: MockSTTProvider | None = None) -> tuple[VoiceEngine, VoiceChatService]:
    """Build a hermetic voice engine + chat service for the tests."""
    engine = VoiceEngine(
        stt=stt or MockSTTProvider(),
        tts=MockTTSProvider(),
        wake_words=["beru"],
    )
    chat = ChatService(
        IntelligenceEngine(MockProvider(), get_agent_registry()),
        ConversationService(),
        ConversationMemory(),
    )
    return engine, VoiceChatService(voice=engine, chat=chat)


async def test_voice_chain_audio_turn_persists_conversation(db_session):
    settings = get_settings()
    stt = MockSTTProvider()
    audio = b"utterance-audio"
    stt.feed(audio, "beru what time is it")
    engine, service = _chain(stt)
    session = await engine.create_session()

    outcome = await service.converse(
        db_session, settings, session_id=session.session_id, audio=audio
    )

    assert outcome.utterance is not None
    assert outcome.utterance.text == "beru what time is it"
    assert outcome.utterance.was_wake is True
    assert outcome.utterance.remainder == "what time is it"
    assert "BERU (mock) received: what time is it" in outcome.reply
    assert outcome.provider == "mock"
    assert outcome.simulated is True
    assert outcome.clip.format == "wav"
    assert len(outcome.clip.audio) > 44
    assert outcome.conversation_id
    assert engine.get_session(session.session_id).conversation_id == outcome.conversation_id

    conversations = ConversationService()
    conversation = await conversations.get(db_session, outcome.conversation_id)
    assert conversation is not None
    messages = await conversations.get_messages(db_session, outcome.conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_voice_chain_runs_the_agent_tool_loop(db_session):
    """The chain exercises the real agent loop: the mock LLM requests the clock
    tool, it genuinely executes (read_clock is default-allowed), and the final
    reply is grounded in the tool result."""
    settings = get_settings()
    engine, service = _chain()
    session = await engine.create_session()

    outcome = await service.converse(
        db_session, settings, session_id=session.session_id, text="beru clock please"
    )

    assert outcome.tool_calls_made == 1
    assert outcome.reply
    assert len(outcome.clip.audio) > 44
    conversations = ConversationService()
    messages = await conversations.get_messages(db_session, outcome.conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_voice_chain_text_mode_uses_full_utterance(db_session):
    settings = get_settings()
    engine, service = _chain()
    session = await engine.create_session()

    outcome = await service.converse(
        db_session, settings, session_id=session.session_id, text="where is the moon"
    )

    assert outcome.utterance.text == "where is the moon"
    assert outcome.utterance.was_wake is False
    assert "where is the moon" in outcome.reply


async def test_voice_chain_continues_the_same_conversation(db_session):
    settings = get_settings()
    engine, service = _chain()
    session = await engine.create_session()

    first = await service.converse(
        db_session, settings, session_id=session.session_id, text="beru hello"
    )
    second = await service.converse(
        db_session, settings, session_id=session.session_id, text="beru are you there"
    )

    assert first.conversation_id == second.conversation_id
    conversations = ConversationService()
    messages = await conversations.get_messages(db_session, first.conversation_id)
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]


async def test_voice_chain_requires_exactly_one_input(db_session):
    settings = get_settings()
    engine, service = _chain()
    session = await engine.create_session()

    with pytest.raises(BadRequestError):
        await service.converse(db_session, settings, session_id=session.session_id)
    with pytest.raises(BadRequestError):
        await service.converse(
            db_session,
            settings,
            session_id=session.session_id,
            audio=b"x",
            text="hello",
        )


async def test_voice_chain_fails_honestly_on_empty_transcript(db_session):
    """Unknown audio transcribes to nothing and the chain must say so rather
    than fabricate a reply."""
    settings = get_settings()
    engine, service = _chain(MockSTTProvider())  # default_text = ""
    session = await engine.create_session()

    with pytest.raises(BadRequestError):
        await service.converse(
            db_session, settings, session_id=session.session_id, audio=b"unknown"
        )
    assert session.transcript  # the empty utterance IS recorded, honestly


async def test_api_converse_audio_turn(client):
    import backend.api.routers.voice as voice_mod

    voice_mod._engine = None
    created = await client.post("/api/v1/voice/sessions", json={})
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    engine = voice_mod.get_voice_engine()
    audio = b"api-audio"
    engine._stt.feed(audio, "beru how are you")

    response = await client.post(
        "/api/v1/voice/converse",
        json={"session_id": session_id, "audio": base64.b64encode(audio).decode()},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["provider"] == "mock"
    assert body["simulated"] is True
    assert body["utterance"]["was_wake"] is True
    assert body["utterance"]["remainder"] == "how are you"
    assert "BERU (mock) received: how are you" in body["reply"]
    assert body["clip"]["bytes"] > 44
    assert body["conversation_id"]

    detail = await client.get(f"/api/v1/conversations/{body['conversation_id']}")
    assert detail.status_code == 200
    roles = [m["role"] for m in detail.json()["messages"]]
    assert roles == ["user", "assistant"]


async def test_api_converse_text_turn_continues_conversation(client):
    import backend.api.routers.voice as voice_mod

    voice_mod._engine = None
    created = await client.post("/api/v1/voice/sessions", json={})
    session_id = created.json()["session_id"]

    first = await client.post(
        "/api/v1/voice/converse",
        json={"session_id": session_id, "text": "beru hello there"},
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/api/v1/voice/converse",
        json={"session_id": session_id, "text": "beru still here"},
    )
    assert second.status_code == 201, second.text
    assert first.json()["conversation_id"] == second.json()["conversation_id"]

    detail = await client.get(f"/api/v1/conversations/{first.json()['conversation_id']}")
    roles = [m["role"] for m in detail.json()["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]


async def test_api_converse_rejects_both_audio_and_text(client):
    import backend.api.routers.voice as voice_mod

    voice_mod._engine = None
    created = await client.post("/api/v1/voice/sessions", json={})
    session_id = created.json()["session_id"]

    response = await client.post(
        "/api/v1/voice/converse",
        json={"session_id": session_id, "audio": "AAAA", "text": "hi"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "http_error"


async def test_api_converse_empty_utterance_is_honest_400(client):
    import backend.api.routers.voice as voice_mod

    voice_mod._engine = None
    created = await client.post("/api/v1/voice/sessions", json={})
    session_id = created.json()["session_id"]

    response = await client.post(
        "/api/v1/voice/converse",
        json={"session_id": session_id, "audio": base64.b64encode(b"unmapped").decode()},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "bad_request"


async def test_api_converse_unknown_session_404(client):
    response = await client.post(
        "/api/v1/voice/converse",
        json={"session_id": "missing", "text": "hello"},
    )
    assert response.status_code == 404