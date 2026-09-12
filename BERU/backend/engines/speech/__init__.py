"""Speech providers: speech-to-text (STT) and text-to-speech (TTS).

Defines the provider protocols used by :class:`VoiceEngine` plus hermetic mock
implementations that need no OS audio hardware or network. Real providers
(whisper, edge-tts, ...) implement the same protocols and are selected via the
``voice_stt_provider`` / ``voice_tts_provider`` settings. First-party plugins
live in the :mod:`backend.engines.speech.whisper` and
:mod:`backend.engines.speech.edge_tts` submodules and register as providers
through the ``beru.stt_providers`` / ``beru.tts_providers`` entry-point groups.
"""

from __future__ import annotations

import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from backend.core.config import Settings, get_settings
from backend.plugins import discovery

#: Wave audio constants (RIFF/WAVE, PCM).
_WAV_HEADER_SIZE = 44

#: Average bitrate assumed for MP3-encoded speech (edge-tts default).
_MP3_BITRATE_BPS = 48_000


class STTProvider(Protocol):
    """Converts raw audio bytes into transcribed text."""

    async def transcribe(self, audio: bytes) -> str:
        """Return the text transcription for ``audio``."""
        ...


class TTSProvider(Protocol):
    """Converts text into audio bytes.

    Implementations return WAV PCM by default. Providers that emit a different
    encoding (e.g. MP3 from edge-tts) declare a class attribute ``format``
    (``"wav"``, ``"mp3"``, ...) so callers serve and measure clips honestly;
    the default is ``"wav"``.
    """

    async def synthesize(self, text: str, **kwargs: object) -> bytes:
        """Return encoded audio for ``text`` (see ``format``)."""
        ...


@dataclass
class AudioSpec:
    """Describes the audio format of a synthesized clip."""

    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2  # bytes per sample (16-bit PCM)


def encode_wav(
    pcm: bytes,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Wrap raw PCM samples in a minimal RIFF/WAVE container."""
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm)

    header = bytearray()
    header += b"RIFF"
    header += struct.pack("<I", _WAV_HEADER_SIZE - 8 + data_size)
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<I", 16)            # fmt chunk size
    header += struct.pack("<H", 1)             # PCM format
    header += struct.pack("<H", channels)      # channel count
    header += struct.pack("<I", sample_rate)   # samples per second
    header += struct.pack("<I", byte_rate)     # bytes per second
    header += struct.pack("<H", block_align)   # block alignment
    header += struct.pack("<H", sample_width * 8)  # bits per sample
    header += b"data"
    header += struct.pack("<I", data_size)

    return bytes(header) + pcm


def encode_silence(
    duration_ms: float,
    spec: AudioSpec | None = None,
) -> bytes:
    """Build a WAV clip of silent PCM for ``duration_ms`` milliseconds."""
    spec = spec or AudioSpec()
    n_samples = int(spec.sample_rate * duration_ms / 1000)
    pcm = bytes(n_samples * spec.channels * spec.sample_width)
    return encode_wav(
        pcm,
        sample_rate=spec.sample_rate,
        channels=spec.channels,
        sample_width=spec.sample_width,
    )


class MockSTTProvider:
    """Hermetic STT: maps audio bytes to text via explicit ``feed`` calls.

    Audio recorded for a phrase via :meth:`feed` returns that phrase on
    :meth:`transcribe`; unknown audio returns ``default_text``.
    """

    def __init__(self, default_text: str = "") -> None:
        self.default_text = default_text
        self._mappings: dict[bytes, str] = {}

    def feed(self, audio: bytes, text: str) -> None:
        """Record the transcription for a specific audio snippet."""
        self._mappings[audio] = text

    async def transcribe(self, audio: bytes) -> str:
        return self._mappings.get(audio, self.default_text)


class MockTTSProvider:
    """Hermetic TTS: returns a deterministic WAV clip per input text.

    The same input text always produces the same audio bytes, so tests can
    assert on stable identifiers and headers without OS audio output.
    """

    def __init__(self, spec: AudioSpec | None = None) -> None:
        self.spec = spec or AudioSpec()
        self._clips: dict[str, bytes] = {}

    async def synthesize(self, text: str, **kwargs: object) -> bytes:
        duration_ms = float(kwargs.get("duration_ms", 100))
        if text not in self._clips:
            self._clips[text] = encode_silence(
                duration_ms, spec=self.spec
            )
        return self._clips[text]


def _clip_duration_ms(audio: bytes, spec: AudioSpec, format: str) -> float:
    """Estimate a clip's duration in ms for reporting.

    WAV (uncompressed PCM) duration is exact from the payload length. For
    compressed formats (``"mp3"``) the byte length is converted at a nominal
    average bitrate, since the container carries no PCM payload length.
    """
    if format == "mp3":
        if not audio:
            return 0.0
        return round(len(audio) * 8.0 / _MP3_BITRATE_BPS * 1000.0, 1)
    payload_seconds = len(audio) / (
        spec.sample_rate * spec.channels * spec.sample_width
    )
    return round(payload_seconds * 1000, 1)


@dataclass
class AudioClip:
    """A synthesized speech output, addressable by ``audio_id``."""

    id: str
    text: str
    audio: bytes
    spec: AudioSpec = field(default_factory=AudioSpec)
    format: str = "wav"
    interrupted: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "format": self.format,
            "sample_rate": self.spec.sample_rate,
            "channels": self.spec.channels,
            "bytes": len(self.audio),
            "duration_ms": _clip_duration_ms(self.audio, self.spec, self.format),
            "interrupted": self.interrupted,
            "created_at": self.created_at.isoformat(),
        }


def new_clip(
    text: str,
    audio: bytes,
    spec: AudioSpec,
    *,
    format: str = "wav",
) -> AudioClip:
    """Build an :class:`AudioClip` with a fresh id."""
    return AudioClip(
        id=uuid.uuid4().hex[:8],
        text=text,
        audio=audio,
        spec=spec,
        format=format,
    )


def _sapi_tts_factory(settings: Settings) -> TTSProvider:
    """Build the Windows SAPI provider (imported lazily; Windows-only)."""
    from backend.engines.speech.sapi import build_sapi_tts

    return build_sapi_tts(settings)


BUILTIN_STT_FACTORIES: dict[str, Callable[[Settings], STTProvider]] = {
    "mock": lambda settings: MockSTTProvider(),
}

BUILTIN_TTS_FACTORIES: dict[str, Callable[[Settings], TTSProvider]] = {
    "mock": lambda settings: MockTTSProvider(),
    # First-party Windows TTS (SAPI). Built in so a source checkout works as-is on
    # Windows; constructing it imports win32com and fails honestly where absent.
    "sapi": _sapi_tts_factory,
}


def build_stt_provider(
    provider: str, settings: Settings | None = None
) -> STTProvider:
    """Construct an STT provider from its configured name.

    Built-in providers (``mock``) act as the fallback; additional names resolve
    through the ``beru.stt_providers`` entry-point group.
    """
    settings = settings or get_settings()
    providers = discovery.merge_with_builtins(
        discovery.GROUP_STT, BUILTIN_STT_FACTORIES
    )
    name = provider.lower().strip()
    factory = providers.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown STT provider '{provider}'. "
            f"Supported: {', '.join(sorted(providers))}."
        )
    stt = factory(settings)
    if not hasattr(stt, "transcribe"):
        raise ValueError(
            f"STT provider '{name}' did not construct an STTProvider "
            f"(got {type(stt).__name__})."
        )
    return stt


def build_tts_provider(
    provider: str, settings: Settings | None = None
) -> TTSProvider:
    """Construct a TTS provider from its configured name.

    Built-in providers (``mock``) act as the fallback; additional names resolve
    through the ``beru.tts_providers`` entry-point group.
    """
    settings = settings or get_settings()
    providers = discovery.merge_with_builtins(
        discovery.GROUP_TTS, BUILTIN_TTS_FACTORIES
    )
    name = provider.lower().strip()
    factory = providers.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown TTS provider '{provider}'. "
            f"Supported: {', '.join(sorted(providers))}."
        )
    tts = factory(settings)
    if not hasattr(tts, "synthesize"):
        raise ValueError(
            f"TTS provider '{name}' did not construct a TTSProvider "
            f"(got {type(tts).__name__})."
        )
    return tts