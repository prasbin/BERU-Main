"""Opt-in live-hardware validation suite (real browser, desktop, voice hardware).

The default suite is fully hermetic and never touches the real OS (no browser,
no screen capture, no clipboard, no speech synthesizer). These tests exercise
the REAL Windows/desktop interfaces through BERU's engines and tools only when
the operator explicitly opts in:

    BERU_LIVE_HW_TESTS=1   explicit opt-in gate (like BERU_REAL_LLM_SMOKE)

They deliberately do not need credentials: they use the loopback network
interface, the real desktop session, and the built-in Windows SAPI voice. A
host without the relevant capability (no Playwright browser, headless screen,
unsupported clipboard) skips or fails honestly — never fakes a pass.

Running the voice test requires Windows with a SAPI voice (pywin32 present via
the ``voice`` extra). Running the browser test requires Playwright Chromium
installed (``python -m playwright install chromium``).
"""

from __future__ import annotations

import importlib.util
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.core.config import Settings
from backend.engines.browser import BrowserAction, get_browser_engine
from backend.engines.clipboard import get_clipboard_engine
from backend.engines.screenshot import get_screenshot_engine
from backend.engines.speech import build_tts_provider
from backend.engines.speech.whisper import build_whisper_stt
from backend.engines.voice import VoiceEngine
from backend.tools.voice import VoiceListenTool, VoiceSpeakTool, build_engine


def _opt_in() -> bool:
    return os.environ.get("BERU_LIVE_HW_TESTS", "").strip() == "1"


def _require_live_hw() -> None:
    if not _opt_in():
        pytest.skip(
            "Live-hardware tests are opt-in: set BERU_LIVE_HW_TESTS=1 "
            "(see tests/test_live_hardware.py)."
        )


# --------------------------------------------------------------------------- #
# Helper: a tiny loopback HTTP server so the browser exercises a real
# navigation without any external network dependency.
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
        body = (
            b"<!doctype html><html><head><title>Live HW Probe</title></head>"
            b"<body><h1 id='heading'>beru live probe</h1>"
            b"<a href='/next'>next</a></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture(scope="module")
def loopback_page_url() -> str:
    _require_live_hw()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Browser (real Playwright Chromium)
# --------------------------------------------------------------------------- #

async def test_live_browser_navigates_and_extracts_text(loopback_page_url: str) -> None:
    _require_live_hw()
    engine = get_browser_engine()
    page = await engine.create_page("about:blank")
    try:
        assert page.status.value == "ready", page
        nav = await engine.execute_action(
            BrowserAction.NAVIGATE, url=loopback_page_url
        )
        assert nav.success, nav.error
        assert "Live HW Probe" in nav.data.get("title", "")

        text = await engine.execute_action(BrowserAction.EXTRACT_TEXT)
        assert text.success, text.error
        assert "beru live probe" in text.data.get("text", "")

        shot = await engine.execute_action(BrowserAction.SCREENSHOT)
        assert shot.success, shot.error
        assert shot.data.get("path")
        assert os.path.getsize(shot.data["path"]) > 0
    finally:
        await engine.close_browser()


# --------------------------------------------------------------------------- #
# Desktop (real screen + real clipboard)
# --------------------------------------------------------------------------- #

def test_live_desktop_screenshot_captures_real_screen() -> None:
    _require_live_hw()
    result = get_screenshot_engine().capture()
    assert result.error is None, result.error
    assert result.width > 0 and result.height > 0
    assert result.path and os.path.getsize(result.path) > 0
    with open(result.path, "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"


def test_live_clipboard_roundtrip_restores_original() -> None:
    _require_live_hw()
    clipboard = get_clipboard_engine()
    original = clipboard.read()
    assert original.error is None, original.error

    sentinel = f"beru-live-hw-{os.getpid()}"
    try:
        written = clipboard.write(sentinel)
        assert written.error is None, written.error
        read_back = clipboard.read()
        assert read_back.error is None, read_back.error
        assert read_back.content == sentinel, "clipboard did not echo the sentinel"
    finally:
        if original.content:
            clipboard.write(original.content)
        else:
            clipboard.write("")


# --------------------------------------------------------------------------- #
# Voice (real Windows SAPI synthesis through the real voice chain)
# --------------------------------------------------------------------------- #

async def test_live_sapi_engine_synthesizes_real_wav() -> None:
    """Real SAPI synthesis yields a genuine WAV clip through the voice engine."""
    _require_live_hw()
    settings = Settings(BERU_VOICE_TTS_PROVIDER="sapi")
    engine = build_engine(settings)
    status = engine.status()
    assert status["tts_provider"] == "SapiTTSProvider"

    session = await engine.create_session()
    clip = await engine.respond(session.session_id, "live hardware voice validation")
    assert clip.format == "wav"
    assert len(clip.audio) > 44  # RIFF + fmt chunk + data
    assert clip.audio[:4] == b"RIFF" and clip.audio[8:12] == b"WAVE"
    assert clip.to_dict()["bytes"] == len(clip.audio)
    assert engine.status()["sessions"] == 1


async def test_live_voice_speak_tool_returns_real_clip() -> None:
    """The VoiceSpeakTool round-trips live SAPI output through the tool edge."""
    _require_live_hw()
    settings = Settings(BERU_VOICE_TTS_PROVIDER="sapi")
    engine = build_engine(settings)
    tool = VoiceSpeakTool(engine=engine)
    result = await tool.run(text="tool edge voice check")
    assert result.ok is True, result.error
    assert result.output["format"] == "wav"
    assert result.output["bytes"] > 44
    assert result.output["id"]


async def test_live_voice_listen_fails_honestly_without_real_stt() -> None:
    """No real STT is configured, so the tool must say so rather than fake it."""
    _require_live_hw()
    # SAPI is only a TTS provider; STT remains the mock simulation, so the
    # listen tool must report its limitation honestly on a live-hardware run.
    settings = Settings(BERU_VOICE_TTS_PROVIDER="sapi")
    engine = build_engine(settings)
    tool = VoiceListenTool(engine=engine)
    result = await tool.run(session_id="whatever")
    assert result.ok is False
    assert "limited" in result.error.lower()


async def test_live_whisper_transcribes_real_speech_clip() -> None:
    """Real whisper STT transcribes audio produced by real SAPI TTS.

    The full real speech chain runs on hardware: synthesize with Windows SAPI,
    buffer the clip as the "captured" audio, and let ``voice_listen`` transcribe
    it with whisper. Skipped when openai-whisper is not installed. The model
    size is overridable with ``BERU_LIVE_STT_MODEL`` (default ``tiny``).
    """
    _require_live_hw()
    if importlib.util.find_spec("whisper") is None:
        pytest.skip("openai-whisper not installed (pip install -e .[voice])")

    model = (os.environ.get("BERU_LIVE_STT_MODEL", "") or "tiny").strip() or "tiny"
    settings = Settings(
        BERU_VOICE_STT_PROVIDER="whisper",
        BERU_VOICE_STT_MODEL=model,
        BERU_VOICE_TTS_PROVIDER="sapi",
    )
    # Source checkouts don't install BERU's own entry points, so construct the
    # first-party provider directly for the live run (the plugin-discovery path
    # itself is covered hermetically in tests/test_voice.py).
    engine = VoiceEngine(
        stt=build_whisper_stt(settings),
        tts=build_tts_provider("sapi", settings),
    )
    assert engine.status()["stt_provider"] == "WhisperSTTProvider"

    session = await engine.create_session()
    clip = await engine.respond(
        session.session_id, "hello this is the live transcription test"
    )
    await engine.ingest_audio(session.session_id, clip.audio)

    tool = VoiceListenTool(engine=engine)
    result = await tool.run(session_id=session.session_id)
    assert result.ok is True, result.error
    text = result.output["text"].strip()
    assert text, "whisper returned no text for the synthesized clip"
    assert result.output["was_wake"] is False