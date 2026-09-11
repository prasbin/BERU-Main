"""Whisper speech-to-text plugin.

A first-party provider selected with ``BERU_VOICE_STT_PROVIDER=whisper`` and
advertised through the ``beru.stt_providers`` entry-point group. Requires the
``beru[voice]`` extra (``openai-whisper``); the heavy import happens lazily so
an install without the extra never pays for it.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from backend.core.config import Settings


class WhisperSTTProvider:
    """Transcribes raw audio bytes with a locally-run whisper model.

    ``openai-whisper`` is imported on construction (when the provider is
    actually selected), and the blocking transcription runs in a worker
    thread so the async call site is never blocked.
    """

    name = "whisper"

    def __init__(self, model_name: str = "base", language: str | None = None) -> None:
        import whisper  # lazy: only installed with the 'voice' extra

        self._model_name = model_name
        self._language = language
        self._model = whisper.load_model(model_name)

    async def transcribe(self, audio: bytes) -> str:
        return await asyncio.to_thread(self._transcribe_blocking, audio)

    def _transcribe_blocking(self, audio: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(audio)
            path = handle.name
        try:
            result = self._model.transcribe(path, language=self._language)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return (result or {}).get("text", "") or ""


def build_whisper_stt(settings: Settings) -> WhisperSTTProvider:
    """Entry-point factory: construct the provider from application settings."""
    return WhisperSTTProvider(model_name=settings.voice_stt_model)