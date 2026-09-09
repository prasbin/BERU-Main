"""Tests for the voice system: wake word, STT/TTS, sessions, interruption, API."""

from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from backend.engines.speech import (
    MockSTTProvider,
    MockTTSProvider,
    encode_silence,
    encode_wav,
)
from backend.engines.voice import (
    VoiceEngine,
    VoiceState,
    WakeWordDetector,
    normalize_text,
)
from backend.main import create_app

# ---- Wake word detector ----


def test_normalize_text():
    assert normalize_text("  Hello, WORLD!  ") == "hello world"
    assert normalize_text("") == ""


def test_wake_word_detects_at_start():
    detector = WakeWordDetector(["beru"])
    assert detector.detect("beru open the calendar") == "beru"


def test_wake_word_case_and_punctuation_insensitive():
    detector = WakeWordDetector(["beru"])
    assert detector.detect("Beru, what time is it?") == "beru"


def test_wake_word_not_detected_mid_sentence():
    detector = WakeWordDetector(["beru"])
    assert detector.detect("hello beru can you hear me") is None


def test_wake_word_alone():
    detector = WakeWordDetector(["beru"])
    assert detector.detect("beru.") == "beru"


def test_wake_word_multiple_candidates():
    detector = WakeWordDetector(["beru", "hey beru"])
    assert detector.detect("hey beru how are you") == "hey beru"


def test_strip_wake_removes_leading_word():
    detector = WakeWordDetector(["beru"])
    assert detector.strip_wake("Beru, open the calendar") == "open the calendar"


def test_strip_wake_no_wake_returns_text():
    detector = WakeWordDetector(["beru"])
    assert detector.strip_wake("tell me a joke") == "tell me a joke"


# ---- Speech providers ----


async def test_mock_stt_feed_and_transcribe():
    provider = MockSTTProvider(default_text="nothing")
    audio = b"\x00\x01\x02"
    provider.feed(audio, "beru hello")
    assert await provider.transcribe(audio) == "beru hello"
    assert await provider.transcribe(b"\x99") == "nothing"


async def test_mock_tts_deterministic():
    provider = MockTTSProvider()
    clip_a = await provider.synthesize("hello")
    clip_b = await provider.synthesize("hello")
    assert clip_a == clip_b
    assert clip_a.startswith(b"RIFF")


def test_encode_wav_header():
    import struct

    audio = encode_wav(b"\x00" * 160, sample_rate=8000, channels=1, sample_width=2)
    assert audio[:4] == b"RIFF"
    assert audio[8:12] == b"WAVE"
    assert audio[24:28] == struct.pack("<I", 8000)  # sample rate little-endian


def test_encode_silence_duration():
    audio = encode_silence(100, spec=None)  # 100 ms @ 16kHz mono 16-bit
    # 1600 samples * 2 bytes/sample + 44-byte header
    assert len(audio) == 44 + 1600 * 2
    assert audio[:4] == b"RIFF"


# ---- Engine: sessions ----


async def test_engine_create_and_list_sessions():
    engine = VoiceEngine()
    session = await engine.create_session()
    assert session.state == VoiceState.LISTENING
    assert engine.session_count() == 1
    assert engine.list_sessions()[0]["session_id"] == session.session_id


async def test_engine_end_session():
    engine = VoiceEngine()
    session = await engine.create_session()
    assert engine.end_session(session.session_id) is True
    assert engine.end_session(session.session_id) is False
    assert engine.session_count() == 0


async def test_engine_unknown_requires_raises():
    engine = VoiceEngine()
    with pytest.raises(KeyError):
        await engine.finalize_utterance("missing")


async def test_engine_purge_expired():
    engine = VoiceEngine(session_timeout=1.0)
    await engine.create_session()
    # No expiry immediately.
    assert engine.purge_expired() == 0
    # Force idle: backdate last_activity past the timeout.
    session = engine.list_sessions()[0]
    from datetime import datetime, timedelta, timezone

    voice_session = engine.get_session(session["session_id"])
    voice_session.last_activity = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert engine.purge_expired() == 1


# ---- Engine: continuous listening / STT ----


async def test_engine_ingest_and_finalize():
    engine = VoiceEngine(stt=MockSTTProvider(default_text="hello there"))
    session = await engine.create_session()
    assert await engine.ingest_audio(session.session_id, b"\x00" * 16) is True
    record = await engine.finalize_utterance(session.session_id)
    assert record is not None
    assert record.text == "hello there"
    assert record.was_wake is False
    assert session.state == VoiceState.LISTENING


async def test_engine_finalize_empty_buffer_returns_none():
    engine = VoiceEngine()
    session = await engine.create_session()
    assert await engine.finalize_utterance(session.session_id) is None


async def test_engine_wake_word_recorded():
    provider = MockSTTProvider()
    audio = b"wake-audio"
    provider.feed(audio, "beru open the calendar")
    engine = VoiceEngine(stt=provider, wake_words=["beru"])
    session = await engine.create_session()
    await engine.ingest_audio(session.session_id, audio)
    record = await engine.finalize_utterance(session.session_id)
    assert record.was_wake is True
    assert record.wake_word == "beru"
    assert record.remainder == "open the calendar"


async def test_engine_transcribe_text_direct():
    engine = VoiceEngine(wake_words=["beru"])
    session = await engine.create_session()
    record = await engine.transcribe_text(session.session_id, "beru set a timer")
    assert record.was_wake is True
    assert record.remainder == "set a timer"


async def test_ingest_rejected_during_processing():
    class HoldingSTT:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, audio: bytes) -> str:
            self.started.set()
            await self.release.wait()
            return "slow transcription"

    engine = VoiceEngine(stt=HoldingSTT())
    session = await engine.create_session()
    await engine.ingest_audio(session.session_id, b"\x00" * 8)

    task = asyncio.create_task(engine.finalize_utterance(session.session_id))
    await engine._stt.started.wait()
    assert await engine.ingest_audio(session.session_id, b"\x01" * 8) is False
    engine._stt.release.set()
    await task
    assert session.state == VoiceState.LISTENING


# ---- Engine: TTS / interruption ----


async def test_engine_respond_creates_clip():
    engine = VoiceEngine()
    session = await engine.create_session()
    clip = await engine.respond(session.session_id, "hello from beru")
    assert clip.id
    assert clip.audio.startswith(b"RIFF")
    assert session.state == VoiceState.LISTENING


async def test_engine_respond_records_clip_in_session():
    engine = VoiceEngine()
    session = await engine.create_session()
    clip = await engine.respond(session.session_id, "hi")
    assert session.clips[0].id == clip.id
    assert len(session.clips) == 1


async def test_engine_interrupt_during_speech():
    class HoldingTTS:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def synthesize(self, text: str, **kwargs: object) -> bytes:
            self.started.set()
            await self.release.wait()
            return encode_wav(b"")

    engine = VoiceEngine(tts=HoldingTTS())
    session = await engine.create_session()
    task = asyncio.create_task(engine.respond(session.session_id, "long speech"))
    await engine._tts.started.wait()
    assert session.state == VoiceState.SPEAKING
    assert engine.interrupt(session.session_id) is True
    assert session.state == VoiceState.LISTENING
    engine._tts.release.set()
    clip = await task
    assert clip.interrupted is True


async def test_engine_interrupt_idle_returns_false():
    engine = VoiceEngine()
    session = await engine.create_session()
    assert engine.interrupt(session.session_id) is False


async def test_engine_status():
    engine = VoiceEngine(wake_words=["beru"])
    status = engine.status()
    assert status["sessions"] == 0
    assert status["wake_words"] == ["beru"]
    assert "stt_provider" in status
    assert "tts_provider" in status


# ---- Tools ----


async def test_voice_listen_tool_unavailable():
    from backend.tools.voice import VoiceListenTool

    tool = VoiceListenTool()
    result = await tool.run(session_id="abc")
    assert result.ok is False
    assert "unavailable" in result.error
    assert tool.requires_confirmation is False


async def test_voice_speak_tool_unavailable():
    from backend.tools.voice import VoiceSpeakTool

    tool = VoiceSpeakTool()
    result = await tool.run(text="hello")
    assert result.ok is False
    assert "unavailable" in result.error
    assert tool.requires_confirmation is True


async def test_core_agent_has_voice_tools():
    from backend.agents.registry import get_agent_registry

    core = get_agent_registry().get("beru_core")
    tool_names = [t.name for t in core._tools.values()]
    assert "voice_listen" in tool_names
    assert "voice_speak" in tool_names


# ---- API ----


@pytest.fixture
def voice_client():
    import backend.api.routers.voice as voice_mod

    voice_mod._engine = None
    client = TestClient(create_app())
    yield client
    voice_mod._engine = None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_api_create_session(voice_client):
    response = voice_client.post("/api/v1/voice/sessions", json={})
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "listening"
    assert body["wake_words"] == ["beru"]


def test_api_list_and_end_session(voice_client):
    session = voice_client.post("/api/v1/voice/sessions", json={}).json()
    assert voice_client.get("/api/v1/voice/sessions").status_code == 200
    delete = voice_client.delete(f"/api/v1/voice/sessions/{session['session_id']}")
    assert delete.status_code == 204
    assert voice_client.delete(f"/api/v1/voice/sessions/{session['session_id']}").status_code == 404


def test_api_listen_transcribes(voice_client):
    session = voice_client.post("/api/v1/voice/sessions", json={}).json()
    sid = session["session_id"]

    import backend.api.routers.voice as voice_mod

    engine = voice_mod.get_voice_engine()
    audio = b"\x00\x01\x02"
    engine._stt.feed(audio, "hello beru system")

    response = voice_client.post(
        f"/api/v1/voice/listen?session_id={sid}", json={"audio": _b64(audio)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["transcribed"] is True
    assert body["utterance"]["text"] == "hello beru system"


def test_api_transcribe_text_wake(voice_client):
    session = voice_client.post("/api/v1/voice/sessions", json={}).json()

    response = voice_client.post(
        "/api/v1/voice/transcribe-text",
        json={"session_id": session["session_id"], "text": "beru remind me"},
    )
    assert response.status_code == 200
    utterance = response.json()["utterance"]
    assert utterance["was_wake"] is True
    assert utterance["remainder"] == "remind me"


def test_api_respond_and_fetch_audio(voice_client):
    session = voice_client.post("/api/v1/voice/sessions", json={}).json()

    respond = voice_client.post(
        "/api/v1/voice/respond",
        json={"session_id": session["session_id"], "text": "all systems nominal"},
    )
    assert respond.status_code == 201
    clip = respond.json()
    assert clip["bytes"] > 44
    assert clip["interrupted"] is False

    audio_response = voice_client.get(f"/api/v1/voice/audio/{clip['id']}")
    assert audio_response.status_code == 200
    assert audio_response.headers["content-type"] == "audio/wav"
    assert audio_response.content.startswith(b"RIFF")


def test_api_audio_unknown_404(voice_client):
    assert voice_client.get("/api/v1/voice/audio/nope").status_code == 404


def test_api_interrupt(voice_client):
    session = voice_client.post("/api/v1/voice/sessions", json={}).json()
    response = voice_client.post(f"/api/v1/voice/sessions/{session['session_id']}/interrupt")
    assert response.status_code == 200
    assert response.json() == {"interrupted": False}


def test_api_listen_unknown_session_404(voice_client):
    response = voice_client.post(
        "/api/v1/voice/listen?session_id=missing", json={"audio": _b64(b"\x00")}
    )
    assert response.status_code == 404


def test_api_status(voice_client):
    response = voice_client.get("/api/v1/voice/status")
    assert response.status_code == 200
    body = response.json()
    assert body["stt_provider"] == "MockSTTProvider"
    assert body["wake_words"] == ["beru"]


def test_voice_websocket_flow(voice_client):
    with voice_client.websocket_connect("/api/v1/voice/ws/tester") as ws:
        ws.send_json({"type": "text", "text": "beru dim the lights"})
        transcript = ws.receive_json()
        assert transcript["type"] == "transcript"
        assert transcript["utterance"]["was_wake"] is True
        wake = ws.receive_json()
        assert wake["type"] == "wake"
        assert wake["wake_word"] == "beru"
        assert wake["remainder"] == "dim the lights"


def test_voice_websocket_respond(voice_client):
    with voice_client.websocket_connect("/api/v1/voice/ws/tester2") as ws:
        ws.send_json({"type": "respond", "text": "greetings"})
        event = ws.receive_json()
        assert event["type"] == "audio"
        assert event["audio"]["bytes"] > 44


def test_voice_websocket_unknown_frame(voice_client):
    with voice_client.websocket_connect("/api/v1/voice/ws/tester3") as ws:
        ws.send_json({"type": "nonsense"})
        event = ws.receive_json()
        assert event["type"] == "error"