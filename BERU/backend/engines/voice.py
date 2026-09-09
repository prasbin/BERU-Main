"""Voice engine: voice sessions, wake word, continuous listening, interruption.

The engine is provider-agnostic (see :mod:`backend.engines.speech`) and owns the
session lifecycle and state machine. Deployment concerns (audio capture/playback,
network endpoints) live outside the engine so it stays hermetic and testable.
"""

from __future__ import annotations

import enum
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.engines.speech import (
    AudioClip,
    AudioSpec,
    MockSTTProvider,
    MockTTSProvider,
    STTProvider,
    TTSProvider,
    new_clip,
)

logger = logging.getLogger(__name__)

_PUNCT_SPACE = re.compile(r"[^a-z0-9 ]+")
_MULTI_SPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for matching."""
    lowered = text.lower().strip()
    lowered = _PUNCT_SPACE.sub(" ", lowered)
    return _MULTI_SPACE.sub(" ", lowered).strip()


class WakeWordDetector:
    """Detects a configured wake word/phrase at the start of transcribed text."""

    def __init__(self, wake_words: list[str] | None = None) -> None:
        self._wake_words = [
            w
            for w in (wake_words or ["beru"])
            if normalize_text(w)
        ]

    @property
    def wake_words(self) -> list[str]:
        return list(self._wake_words)

    def detect(self, text: str) -> str | None:
        """Return the matched wake word, or ``None`` if not present."""
        normalized = normalize_text(text)
        if not normalized:
            return None
        for word in self._wake_words:
            if normalized == word or normalized.startswith(word + " "):
                return word
        return None

    def strip_wake(self, text: str) -> str:
        """Strip a leading wake word, returning the instruction remainder."""
        matched = self.detect(text)
        if matched is None:
            return text.strip()
        normalized = normalize_text(text)
        remainder = normalized[len(matched):].strip()
        return re.sub(r"^[^a-z0-9]+", "", remainder).strip()


class VoiceState(str, enum.Enum):
    """Lifecycle state of a voice session."""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


@dataclass
class UtteranceRecord:
    """A transcribed utterance captured in a session."""

    text: str
    was_wake: bool = False
    wake_word: str | None = None
    remainder: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        result: dict = {
            "text": self.text,
            "was_wake": self.was_wake,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.wake_word:
            result["wake_word"] = self.wake_word
            result["remainder"] = self.remainder
        return result


@dataclass
class VoiceSession:
    """An active voice interaction, buffering audio and accumulating context."""

    session_id: str
    state: VoiceState = VoiceState.LISTENING
    wake_words: list[str] = field(default_factory=lambda: ["beru"])
    buffer: bytearray = field(default_factory=bytearray)
    transcript: list[UtteranceRecord] = field(default_factory=list)
    clips: list[AudioClip] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "wake_words": list(self.wake_words),
            "buffer_bytes": len(self.buffer),
            "utterances": [u.to_dict() for u in self.transcript],
            "clips": [c.to_dict() for c in self.clips],
            "utterance_count": len(self.transcript),
            "clip_count": len(self.clips),
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
        }


class VoiceEngine:
    """Coordinates STT/TTS providers and per-session voice state.

    Continuous listening: audio is accumulated in a per-session buffer via
    :meth:`ingest_audio`; a finalize call (or silence/timeout from a client)
    transcribes the whole buffer as one utterance.
    """

    def __init__(
        self,
        *,
        stt: STTProvider | None = None,
        tts: TTSProvider | None = None,
        wake_words: list[str] | None = None,
        spec: AudioSpec | None = None,
        session_timeout: float = 300.0,
    ) -> None:
        self._stt = stt or MockSTTProvider()
        self._tts = tts or MockTTSProvider()
        self._spec = spec or AudioSpec()
        self._detector = WakeWordDetector(wake_words)
        self._session_timeout = session_timeout
        self._sessions: dict[str, VoiceSession] = {}

    # ---- sessions ----

    async def create_session(self, wake_words: list[str] | None = None) -> VoiceSession:
        detector = WakeWordDetector(wake_words or self._detector.wake_words)
        session = VoiceSession(
            session_id=uuid.uuid4().hex[:12],
            wake_words=detector.wake_words,
        )
        self._sessions[session.session_id] = session
        logger.info("Voice session created: %s", session.session_id)
        return session

    def get_session(self, session_id: str) -> VoiceSession | None:
        return self._sessions.get(session_id)

    def end_session(self, session_id: str) -> bool:
        removed = self._sessions.pop(session_id, None)
        if removed is not None:
            logger.info("Voice session ended: %s", session_id)
        return removed is not None

    def list_sessions(self) -> list[dict]:
        return [s.to_dict() for s in self._sessions.values()]

    def session_count(self) -> int:
        return len(self._sessions)

    def purge_expired(self, now: datetime | None = None) -> int:
        """Remove sessions idle past ``session_timeout``; return count removed."""
        now = now or datetime.now(timezone.utc)
        expired = [
            sid
            for sid, s in self._sessions.items()
            if (now - s.last_activity).total_seconds() > self._session_timeout
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        return len(expired)

    # ---- continuous listening / STT ----

    async def ingest_audio(self, session_id: str, audio: bytes) -> bool:
        """Append a raw audio chunk to the session buffer.

        Returns ``False`` and ignores the chunk when the session is processing,
        so callers can retry or hold audio until the session is ready.
        """
        session = self._require(session_id)
        if session.state == VoiceState.PROCESSING:
            return False
        session.buffer.extend(audio)
        session.last_activity = datetime.now(timezone.utc)
        return True

    async def finalize_utterance(self, session_id: str) -> UtteranceRecord | None:
        """Transcribe the buffered audio as a single utterance.

        Transitions the session PROCESSING → LISTENING. Returns ``None`` when
        the buffer was empty.
        """
        session = self._require(session_id)
        if not session.buffer:
            return None

        session.state = VoiceState.PROCESSING
        audio = bytes(session.buffer)
        session.buffer.clear()
        try:
            text = await self._stt.transcribe(audio)
        finally:
            session.state = VoiceState.LISTENING

        record = self._record_utterance(session, text)
        session.last_activity = datetime.now(timezone.utc)
        return record

    async def transcribe_text(self, session_id: str, text: str) -> UtteranceRecord:
        """Record text directly (bypasses STT; used by text-mode / scripting)."""
        session = self._require(session_id)
        record = self._record_utterance(session, text)
        session.last_activity = datetime.now(timezone.utc)
        return record

    def _record_utterance(self, session: VoiceSession, text: str) -> UtteranceRecord:
        wake_word = self._detector.detect(text)
        record = UtteranceRecord(
            text=text,
            was_wake=wake_word is not None,
            wake_word=wake_word,
            remainder=self._detector.strip_wake(text) if wake_word else text,
        )
        session.transcript.append(record)
        return record

    # ---- TTS ----

    async def respond(self, session_id: str, text: str) -> AudioClip:
        """Synthesize a spoken response and attach it to the session.

        The session is in :attr:`VoiceState.SPEAKING` while synthesis is in
        flight. If :meth:`interrupt` runs concurrently, the resulting clip is
        marked interrupted and playback is considered cancelled.
        """
        session = self._require(session_id)
        session.state = VoiceState.SPEAKING
        session.last_activity = datetime.now(timezone.utc)
        audio = await self._tts.synthesize(text, duration_ms=100)
        clip = new_clip(text, audio, self._spec)
        if session.state != VoiceState.SPEAKING:
            clip.interrupted = True
        session.clips.append(clip)
        session.state = VoiceState.LISTENING
        return clip

    # ---- interruption ----

    def interrupt(self, session_id: str) -> bool:
        """Cancel active speech. Returns ``True`` if speaking was interrupted."""
        session = self._require(session_id)
        if session.state == VoiceState.SPEAKING:
            session.state = VoiceState.LISTENING
            session.last_activity = datetime.now(timezone.utc)
            return True
        return False

    # ---- status ----

    def status(self) -> dict:
        return {
            "sessions": self.session_count(),
            "stt_provider": type(self._stt).__name__,
            "tts_provider": type(self._tts).__name__,
            "wake_words": self._detector.wake_words,
            "session_timeout": self._session_timeout,
        }

    def _require(self, session_id: str) -> VoiceSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown voice session '{session_id}'.")
        return session