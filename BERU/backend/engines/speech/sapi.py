"""Windows SAPI text-to-speech provider.

A first-party TTS provider selected with ``BERU_VOICE_TTS_PROVIDER=sapi`` and
registered as a built-in TTS factory in :mod:`backend.engines.speech`. It
renders text through the Windows Speech API (SAPI) via pywin32's ``win32com``
bindings into a genuine PCM WAV clip using the system's default voice.

Windows only: the ``win32com`` import happens lazily on construction (when the
provider is actually selected), so non-Windows installs never pay for it and a
missing SAPI fails honestly with a clear error.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from backend.core.config import Settings

#: SAPI audio format token: SAFT16kHz16BitMono (16 kHz, 16-bit, mono PCM).
_SAFT16K16BITMONO = 9
#: SAPI file-stream open mode token: SSFMCreateForWrite.
_SSFM_CREATE_FOR_WRITE = 3


class SapiTTSProvider:
    """Synthesizes speech with the Windows Speech API into a 16 kHz WAV clip."""

    name = "sapi"
    format = "wav"

    def __init__(self, voice: str = "") -> None:
        import win32com.client  # lazy: Windows-only

        self._com = win32com.client
        self._voice = voice

    async def synthesize(self, text: str, **kwargs: object) -> bytes:
        del kwargs  # SAPI speaks naturally; duration/rate follow the real voice
        return await asyncio.to_thread(self._synthesize_blocking, text)

    def _synthesize_blocking(self, text: str) -> bytes:
        import pythoncom  # lazy: Windows-only

        # The worker thread has no COM apartment; initialize one for this call.
        pythoncom.CoInitialize()
        try:
            return self._synthesize_com(text)
        finally:
            pythoncom.CoUninitialize()

    def _synthesize_com(self, text: str) -> bytes:
        voice = self._com.Dispatch("SAPI.SpVoice")
        if self._voice:
            try:
                voice.Voice = self._com.Dispatch("SAPI.SpObjectToken").GetVoice(self._voice)
            except Exception:  # noqa: BLE001 - fall back to the default voice
                pass

        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            stream = self._com.Dispatch("SAPI.SpFileStream")
            stream.Format.Type = _SAFT16K16BITMONO
            stream.Open(path, _SSFM_CREATE_FOR_WRITE)
            old_output = voice.AudioOutputStream
            try:
                voice.AudioOutputStream = stream
                voice.Speak(text)
            finally:
                stream.Close()
                voice.AudioOutputStream = old_output
            with open(path, "rb") as handle:
                return handle.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def build_sapi_tts(settings: Settings) -> SapiTTSProvider:
    """Build the provider from application settings."""
    return SapiTTSProvider(voice=settings.voice_tts_voice)