"""Voice tools for agents: listen (STT) and speak (TTS).

These tools are wired to the real :class:`backend.engines.voice.VoiceEngine`
and its configured STT/TTS providers. They are honest about what the provider
chain can do:

* ``voice_listen`` transcribes audio already buffered in a session through the
  configured STT provider. With the hermetic ``mock`` provider it fails cleanly
  instead of pretending real speech was recognised.
* ``voice_speak`` synthesizes a real clip through the configured TTS provider
  (e.g. Windows SAPI or edge-tts). With ``mock`` it fails cleanly instead of
  claiming speech was spoken.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from backend.core.config import Settings, get_settings
from backend.engines.speech import build_stt_provider, build_tts_provider
from backend.engines.voice import VoiceEngine, VoiceSession
from backend.tools.base import Tool, ToolResult


@lru_cache(maxsize=1)
def _default_engine() -> VoiceEngine:
    """Build (and cache) the process-wide voice engine from current settings."""
    return build_engine(get_settings())


def build_engine(settings: Settings) -> VoiceEngine:
    """Construct a :class:`VoiceEngine` from application settings."""
    stt = build_stt_provider(settings.voice_stt_provider, settings)
    tts = build_tts_provider(settings.voice_tts_provider, settings)
    wake_words = [
        word.strip()
        for word in settings.voice_wake_words.split(",")
        if word.strip()
    ]
    return VoiceEngine(stt=stt, tts=tts, wake_words=wake_words or None)


def _provider_name(engine: VoiceEngine, kind: str) -> str:
    """Return the constructed provider class name (from the engine status)."""
    status = engine.status()
    return str(status.get(f"{kind}_provider", "")) or type(
        engine._stt if kind == "stt" else engine._tts
    ).__name__


class VoiceListenTool(Tool):
    """Capture and transcribe a spoken utterance."""

    name = "voice_listen"
    description = "Capture a spoken utterance and transcribe it (wake-word aware)."
    permissions = ["read"]
    availability = "limited"  # real only when a non-mock STT provider is configured
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The voice session to listen on.",
            },
            "duration_ms": {
                "type": "integer",
                "description": "Optional listen duration hint in milliseconds.",
            },
        },
        "required": [],
    }

    def __init__(self, engine: VoiceEngine | None = None) -> None:
        self._engine = engine

    def _voice_engine(self) -> VoiceEngine:
        return self._engine or _default_engine()

    async def run(self, **kwargs: Any) -> ToolResult:
        engine = self._voice_engine()
        if _provider_name(engine, "stt") == "MockSTTProvider":
            return ToolResult.failure(
                f"Tool '{self.name}' is limited: the configured STT provider is "
                "the 'mock' simulation, so no real speech can be recognised. "
                "Select a real STT provider (e.g. whisper) to enable listening."
            )

        session = await _resolve_session(engine, kwargs.get("session_id"))
        if session is None:
            return ToolResult.failure(
                f"Tool '{self.name}': unknown voice session."
            )
        if not session.buffer:
            return ToolResult.failure(
                f"Tool '{self.name}' found no buffered audio for session "
                f"'{session.session_id}' to transcribe."
            )

        record = await engine.finalize_utterance(session.session_id)
        if record is None:
            return ToolResult.failure("No utterance could be transcribed.")
        return ToolResult.success(record.to_dict())


class VoiceSpeakTool(Tool):
    """Produce a spoken response from text."""

    name = "voice_speak"
    description = "Synthesize a spoken response and play it to the user."
    permissions = ["read"]
    requires_confirmation = True
    availability = "limited"  # real only when a non-mock TTS provider is configured
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to speak aloud.",
            },
            "session_id": {
                "type": "string",
                "description": "The voice session to speak through.",
            },
        },
        "required": ["text"],
    }

    def __init__(self, engine: VoiceEngine | None = None) -> None:
        self._engine = engine

    def _voice_engine(self) -> VoiceEngine:
        return self._engine or _default_engine()

    async def run(self, **kwargs: Any) -> ToolResult:
        text = kwargs.get("text", "")
        if not text or not text.strip():
            return ToolResult.failure(f"Tool '{self.name}' requires non-empty 'text'.")

        engine = self._voice_engine()
        if _provider_name(engine, "tts") == "MockTTSProvider":
            return ToolResult.failure(
                f"Tool '{self.name}' is limited: the configured TTS provider is "
                "the 'mock' simulation, so no real speech can be synthesised. "
                "Select a real TTS provider (e.g. sapi or edge-tts) to enable "
                "spoken responses."
            )

        session = await _resolve_session(engine, kwargs.get("session_id"))
        if session is None:
            return ToolResult.failure(
                f"Tool '{self.name}': unknown voice session."
            )
        clip = await engine.respond(session.session_id, text)
        return ToolResult.success(clip.to_dict())


async def _resolve_session(
    engine: VoiceEngine, session_id: Any
) -> VoiceSession | None:
    """Return the referenced session, creating a new one when none is given."""
    if session_id:
        return engine.get_session(str(session_id))
    return await engine.create_session()