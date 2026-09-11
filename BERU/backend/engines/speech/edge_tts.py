"""Microsoft Edge text-to-speech plugin.

A first-party provider selected with ``BERU_VOICE_TTS_PROVIDER=edge_tts`` and
advertised through the ``beru.tts_providers`` entry-point group. Requires the
``beru[voice]`` extra (``edge-tts``); the heavy import happens lazily so an
install without the extra never pays for it.

Edge voices synthesize MP3, so the provider declares ``format = "mp3"`` and the
voice pipeline serves the clip as ``audio/mp3`` (and estimates duration at the
nominal MP3 bitrate) instead of pretending it is WAV PCM.
"""

from __future__ import annotations

from backend.core.config import Settings


class EdgeTTSProvider:
    """Synthesizes speech through Microsoft Edge's free neural voices."""

    name = "edge_tts"
    format = "mp3"

    def __init__(self, voice: str = "en-US-JennyNeural") -> None:
        import edge_tts  # lazy: only installed with the 'voice' extra

        self._voice = voice
        self._edge_tts = edge_tts

    async def synthesize(self, text: str, **kwargs: object) -> bytes:
        communicate = self._edge_tts.Communicate(text, self._voice)
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                audio.extend(chunk.get("data") or b"")
        return bytes(audio)


def build_edge_tts(settings: Settings) -> EdgeTTSProvider:
    """Entry-point factory: construct the provider from application settings."""
    return EdgeTTSProvider(voice=settings.voice_tts_voice)