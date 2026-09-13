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


async def test_voice_listen_tool_honest_with_mock_stt():
    """With the simulated STT provider, the tool must not claim transcription."""
    from backend.tools.voice import VoiceListenTool

    engine = VoiceEngine(stt=MockSTTProvider(), tts=MockTTSProvider())
    tool = VoiceListenTool(engine=engine)
    result = await tool.run(session_id="abc")
    assert result.ok is False
    assert "mock" in result.error.lower()
    assert tool.requires_confirmation is False


async def test_voice_listen_tool_unknown_session():
    from backend.tools.voice import VoiceListenTool

    class RealSTT:
        async def transcribe(self, audio: bytes) -> str:
            return ""

    engine = VoiceEngine(stt=RealSTT(), tts=MockTTSProvider())
    tool = VoiceListenTool(engine=engine)
    result = await tool.run(session_id="no-such-session")
    assert result.ok is False
    assert "unknown voice session" in result.error.lower()


async def test_voice_listen_tool_transcribes_buffered_audio():
    from backend.tools.voice import VoiceListenTool

    class RealSTT:
        def __init__(self):
            self.calls = []

        async def transcribe(self, audio: bytes) -> str:
            self.calls.append(audio)
            return "open the calendar"

    engine = VoiceEngine(stt=RealSTT(), tts=MockTTSProvider())
    session = await engine.create_session()
    await engine.ingest_audio(session.session_id, encode_silence(200))
    tool = VoiceListenTool(engine=engine)
    result = await tool.run(session_id=session.session_id)
    assert result.ok is True
    assert result.output["text"] == "open the calendar"
    assert not result.output["was_wake"]


async def test_voice_listen_tool_transcribes_with_whisper_plugin(monkeypatch):
    """The whisper plugin factory resolves through discovery, the engine wires
    it in, and the tool's guard lets a real (non-mock) provider transcribe.

    The chain under test is the production path: ``build_stt_provider("whisper")
    -> build_engine -> VoiceListenTool.run``. Whisper itself is faked via
    ``sys.modules`` the same way the plugin unit tests do, so the test stays
    hermetic.
    """
    import sys

    from backend.core.config import Settings
    from backend.engines.speech.whisper import build_whisper_stt
    from backend.plugins import discovery
    from backend.tools.voice import VoiceListenTool, build_engine

    class _FakeWhisperModel:
        def transcribe(self, path, language=None):  # noqa: ANN001, ARG002
            return {"text": "open the calendar"}

    class _FakeWhisperModule:
        def load_model(self, name):  # noqa: ANN001, ARG002
            return _FakeWhisperModel()

    monkeypatch.setitem(sys.modules, "whisper", _FakeWhisperModule())
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"whisper": build_whisper_stt} if group == discovery.GROUP_STT else {},
    )

    settings = Settings(BERU_VOICE_STT_PROVIDER="whisper", BERU_VOICE_TTS_PROVIDER="mock")
    engine = build_engine(settings)
    assert engine.status()["stt_provider"] == "WhisperSTTProvider"
    assert engine.status()["tts_provider"] == "MockTTSProvider"

    session = await engine.create_session()
    await engine.ingest_audio(session.session_id, encode_silence(200))
    tool = VoiceListenTool(engine=engine)
    result = await tool.run(session_id=session.session_id)
    assert result.ok is True, result.error
    assert result.output["text"] == "open the calendar"
    assert not result.output["was_wake"]


async def test_voice_speak_tool_synthesizes_with_edge_tts_plugin(monkeypatch):
    """The edge_tts plugin factory resolves through discovery, the engine wires
    it in, and the tool's guard lets a real (non-mock) provider synthesize.

    The chain under test is the production path: ``build_tts_provider("edge_tts")
    -> build_engine -> VoiceSpeakTool.run``. edge-tts itself is faked via
    ``sys.modules`` the same way the plugin unit tests do, so the test stays
    hermetic and needs no network.
    """
    import sys

    from backend.core.config import Settings
    from backend.engines.speech.edge_tts import build_edge_tts
    from backend.plugins import discovery
    from backend.tools.voice import VoiceSpeakTool, build_engine

    class _FakeCommunicate:
        async def stream(self):
            yield {"type": "audio", "data": b"ID3"}
            yield {"type": "WordBoundary", "data": {"offset": 0}}
            yield {"type": "audio", "data": b"-mp3tail"}

    class _FakeEdgeTTsModule:
        def Communicate(self, text, voice):  # noqa: ANN001, ARG002
            self.calls.append((text, voice))
            return _FakeCommunicate()

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

    fake_module = _FakeEdgeTTsModule()
    monkeypatch.setitem(sys.modules, "edge_tts", fake_module)
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"edge_tts": build_edge_tts} if group == discovery.GROUP_TTS else {},
    )

    settings = Settings(BERU_VOICE_TTS_PROVIDER="edge_tts", BERU_VOICE_STT_PROVIDER="mock")
    engine = build_engine(settings)
    assert engine.status()["stt_provider"] == "MockSTTProvider"
    assert engine.status()["tts_provider"] == "EdgeTTSProvider"

    tool = VoiceSpeakTool(engine=engine)
    result = await tool.run(text="hello there")
    assert result.ok is True, result.error
    assert result.output["text"] == "hello there"
    assert result.output["format"] == "mp3"
    assert result.output["bytes"] == len(b"ID3-mp3tail")
    assert result.output["id"]
    assert fake_module.calls == [("hello there", "en-US-JennyNeural")]


async def test_voice_speak_tool_honest_with_mock_tts():
    """With the simulated TTS provider, the tool must not claim speech."""
    from backend.tools.voice import VoiceSpeakTool

    engine = VoiceEngine(stt=MockSTTProvider(), tts=MockTTSProvider())
    tool = VoiceSpeakTool(engine=engine)
    result = await tool.run(text="hello")
    assert result.ok is False
    assert "mock" in result.error.lower()
    assert tool.requires_confirmation is True


async def test_voice_speak_tool_synthesizes_real_clip():
    from backend.tools.voice import VoiceSpeakTool

    class RealTTS:
        format = "wav"

        async def synthesize(self, text: str, **kwargs: object) -> bytes:
            return encode_silence(300)

    engine = VoiceEngine(stt=MockSTTProvider(), tts=RealTTS())
    tool = VoiceSpeakTool(engine=engine)
    result = await tool.run(text="hello there")
    assert result.ok is True
    assert result.output["text"] == "hello there"
    assert result.output["format"] == "wav"
    assert result.output["id"]


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