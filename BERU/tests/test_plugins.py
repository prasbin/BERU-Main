"""Tests for the Stage 5.3 extension platform.

Covers entry-point based discovery (tools, agents, providers), the
whisper/edge-tts voice plugins, format-aware audio clips, and the packaging
metadata / wheel build that makes third-party plugins installable.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib

import backend.engines.embeddings.registry as embedding_registry
import backend.engines.llm.registry as llm_registry
import backend.plugins.discovery as discovery
from backend import __version__
from backend.agents.base import BaseAgent
from backend.agents.registry import get_agent_registry
from backend.core.config import Settings, get_settings
from backend.core.errors import ConfigurationError
from backend.engines.embeddings.base import EmbeddingProvider
from backend.engines.embeddings.mock import MockEmbeddingProvider
from backend.engines.llm.base import LLMProvider, LLMResponse
from backend.engines.llm.mock import MockProvider
from backend.engines.speech import (
    AudioSpec,
    MockSTTProvider,
    build_stt_provider,
    build_tts_provider,
    new_clip,
)
from backend.engines.speech.edge_tts import EdgeTTSProvider, build_edge_tts
from backend.engines.speech.whisper import WhisperSTTProvider, build_whisper_stt
from backend.engines.voice import VoiceEngine
from backend.tools.base import Tool, ToolResult
from backend.tools.clock import ClockTool
from backend.tools.registry import get_tool_registry

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PLUGIN_FIXTURE = ROOT / "scripts" / "plugin_fixture" / "beru_sample_tool"


class _FakeEntryPoint:
    def __init__(self, name: str, callable_obj: object) -> None:
        self.name = name
        self._callable = callable_obj

    def load(self) -> object:
        return self._callable()


# ---------------------------------------------------------------------------
# Discovery primitives
# ---------------------------------------------------------------------------


def test_load_entry_points_empty_when_not_installed():
    # In the source checkout no distribution declares beru.* groups.
    assert discovery.load_entry_point_factories("beru.tools") == {}


def test_load_entry_points_skips_broken_imports(monkeypatch):
    def broken() -> None:
        raise ImportError("nope")

    entry_points = [
        _FakeEntryPoint("good", lambda: "factory"),
        _FakeEntryPoint("bad", broken),
    ]
    fake = SimpleNamespace(select=lambda group: entry_points)
    monkeypatch.setattr(discovery.metadata, "entry_points", lambda: fake)
    factories = discovery.load_entry_point_factories("beru.any")
    assert factories == {"good": "factory"}


def test_merge_with_builtins_keeps_builtins_winning(monkeypatch):
    builtin_factory = lambda settings: "builtin"  # noqa: E731
    evil_factory = lambda settings: "evil"  # noqa: E731
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"mock": evil_factory, "custom": lambda settings: "custom"},
    )
    merged = discovery.merge_with_builtins("beru.llm_providers", {"mock": builtin_factory})
    assert merged["mock"] is builtin_factory
    assert merged["custom"]("settings") == "custom"


# ---------------------------------------------------------------------------
# Tool registry discovery
# ---------------------------------------------------------------------------


class SampleTool(Tool):
    name = "sample_tool"
    description = "A plugin sample tool."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs) -> ToolResult:
        return ToolResult.success({"ok": True})


def test_tool_registry_discovers_plugin(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"sample_entry": SampleTool},
    )
    get_tool_registry.cache_clear()
    try:
        registry = get_tool_registry()
        names = {tool.name for tool in registry.list()}
        assert "clock" in names
        assert "sample_tool" in names
        assert isinstance(registry.get("sample_tool"), SampleTool)
    finally:
        get_tool_registry.cache_clear()


def test_tool_registry_keeps_builtin_on_collision(monkeypatch):
    class CollisionTool(Tool):
        name = "clock"
        description = "shadow"

        async def run(self, **kwargs) -> ToolResult:
            return ToolResult.success({})

    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"shadow": CollisionTool},
    )
    get_tool_registry.cache_clear()
    try:
        registry = get_tool_registry()
        assert isinstance(registry.get("clock"), ClockTool)
        names = [tool.name for tool in registry.list()]
        assert names.count("clock") == 1
    finally:
        get_tool_registry.cache_clear()


def test_tool_registry_skips_non_tool_plugin(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"not_a_tool": lambda: "nope"},
    )
    get_tool_registry.cache_clear()
    try:
        registry = get_tool_registry()
        assert "clock" in [tool.name for tool in registry.list()]
    finally:
        get_tool_registry.cache_clear()


def test_tool_registry_skips_failing_factory(monkeypatch):
    def explode() -> None:
        raise RuntimeError("factory failure")

    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"broken": explode},
    )
    get_tool_registry.cache_clear()
    try:
        registry = get_tool_registry()
        assert "clock" in [tool.name for tool in registry.list()]
    finally:
        get_tool_registry.cache_clear()


# ---------------------------------------------------------------------------
# Agent registry discovery
# ---------------------------------------------------------------------------


class SampleAgent(BaseAgent):
    name = "sample_agent"
    description = "A plugin sample agent."
    capabilities = ["conversation"]
    default_system_prompt = "You are a plugin agent."


def test_agent_registry_discovers_plugin(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"sample_entry": lambda: SampleAgent()},
    )
    get_agent_registry.cache_clear()
    try:
        registry = get_agent_registry()
        names = [agent.name for agent in registry.list()]
        assert "sample_agent" in names
        assert isinstance(registry.get("sample_agent"), SampleAgent)
    finally:
        get_agent_registry.cache_clear()


def test_agent_registry_keeps_builtin_on_collision(monkeypatch):
    class CollisionAgent(BaseAgent):
        name = "beru_core"
        description = "shadow"

    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"shadow": lambda: CollisionAgent()},
    )
    get_agent_registry.cache_clear()
    try:
        registry = get_agent_registry()
        core = registry.get("beru_core")
        assert core.description != "shadow"
        assert "beru_core" in [agent.name for agent in registry.list()]
    finally:
        get_agent_registry.cache_clear()


# ---------------------------------------------------------------------------
# LLM provider registry
# ---------------------------------------------------------------------------


class FakeLLMProvider(LLMProvider):
    name = "custom_llm"

    def __init__(self, settings: Settings) -> None:
        self.received_settings = settings

    async def chat(self, messages, **kwargs) -> LLMResponse:
        return LLMResponse(content="plugin reply", model=self.name)


def test_llm_registry_uses_plugin_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "custom_llm")
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"custom_llm": FakeLLMProvider},
    )
    get_settings.cache_clear()
    llm_registry.get_llm_provider.cache_clear()
    try:
        provider = llm_registry.get_llm_provider()
        assert isinstance(provider, FakeLLMProvider)
        assert provider.received_settings.llm_provider == "custom_llm"
    finally:
        llm_registry.get_llm_provider.cache_clear()
        get_settings.cache_clear()


def test_llm_registry_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "does_not_exist")
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    get_settings.cache_clear()
    llm_registry.get_llm_provider.cache_clear()
    try:
        with pytest.raises(ConfigurationError, match="Unknown LLM_PROVIDER"):
            llm_registry.get_llm_provider()
    finally:
        llm_registry.get_llm_provider.cache_clear()
        get_settings.cache_clear()


def test_llm_registry_rejects_non_provider_result(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bad_plugin")
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"bad_plugin": lambda settings: "not a provider"},
    )
    get_settings.cache_clear()
    llm_registry.get_llm_provider.cache_clear()
    try:
        with pytest.raises(ConfigurationError, match="did not construct an LLMProvider"):
            llm_registry.get_llm_provider()
    finally:
        llm_registry.get_llm_provider.cache_clear()
        get_settings.cache_clear()


def test_llm_registry_builtin_fallback_still_works(monkeypatch):
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    settings = Settings(llm_provider="mock")
    provider = llm_registry.build_provider(settings)
    assert isinstance(provider, MockProvider)


# ---------------------------------------------------------------------------
# Embedding provider registry
# ---------------------------------------------------------------------------


class FakeEmbeddingProvider(EmbeddingProvider):
    name = "custom_embed"
    dimension = 8

    def __init__(self, settings: Settings) -> None:
        self.received_settings = settings

    async def embed(self, texts) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


def test_embedding_registry_uses_plugin_provider(monkeypatch):
    monkeypatch.setenv("BERU_EMBEDDING_PROVIDER", "custom_embed")
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"custom_embed": FakeEmbeddingProvider},
    )
    get_settings.cache_clear()
    embedding_registry.get_embedding_provider.cache_clear()
    try:
        provider = embedding_registry.get_embedding_provider()
        assert isinstance(provider, FakeEmbeddingProvider)
        assert provider.received_settings.embedding_provider == "custom_embed"
    finally:
        embedding_registry.get_embedding_provider.cache_clear()
        get_settings.cache_clear()


def test_embedding_registry_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("BERU_EMBEDDING_PROVIDER", "does_not_exist")
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    get_settings.cache_clear()
    embedding_registry.get_embedding_provider.cache_clear()
    try:
        with pytest.raises(ConfigurationError, match="Unknown EMBEDDING_PROVIDER"):
            embedding_registry.get_embedding_provider()
    finally:
        embedding_registry.get_embedding_provider.cache_clear()
        get_settings.cache_clear()


def test_embedding_registry_builtin_mock_factory():
    settings = Settings(embedding_provider="mock")
    provider = embedding_registry.BUILTIN_FACTORIES["mock"](settings)
    assert isinstance(provider, MockEmbeddingProvider)
    assert provider.dimension == settings.embedding_dimension


# ---------------------------------------------------------------------------
# STT / TTS provider factories
# ---------------------------------------------------------------------------


class _FakeSTT:
    async def transcribe(self, audio: bytes) -> str:
        return "from plugin stt"


class _FakeTTS:
    format = "mp3"

    async def synthesize(self, text: str, **kwargs) -> bytes:
        return b"plugin-mp3"


def test_build_stt_uses_plugin(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"custom_stt": lambda settings: _FakeSTT()},
    )
    provider = build_stt_provider("custom_stt", settings=Settings())
    assert isinstance(provider, _FakeSTT)


def test_build_tts_uses_plugin(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"custom_tts": lambda settings: _FakeTTS()},
    )
    provider = build_tts_provider("custom_tts", settings=Settings())
    assert isinstance(provider, _FakeTTS)
    assert provider.format == "mp3"


def test_build_stt_passes_settings(monkeypatch):
    captured: dict = {}

    def factory(settings: Settings) -> _FakeSTT:
        captured["settings"] = settings
        return _FakeSTT()

    monkeypatch.setattr(
        discovery,
        "load_entry_point_factories",
        lambda group: {"custom_stt": factory},
    )
    settings = Settings(voice_stt_provider="custom_stt")
    provider = build_stt_provider("custom_stt", settings=settings)
    assert isinstance(provider, _FakeSTT)
    assert captured["settings"] is settings


def test_build_stt_case_insensitive_mock(monkeypatch):
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    provider = build_stt_provider("MOCK", settings=Settings())
    assert isinstance(provider, MockSTTProvider)


def test_build_stt_unknown_raises(monkeypatch):
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    with pytest.raises(ValueError, match="Unknown STT provider"):
        build_stt_provider("missing", settings=Settings())


def test_build_tts_unknown_raises(monkeypatch):
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    with pytest.raises(ValueError, match="Unknown TTS provider"):
        build_tts_provider("missing", settings=Settings())


def test_build_stt_unknown_first_party_plugin_gets_actionable_hint(monkeypatch):
    """An unregistered first-party plugin name explains the real remedy."""
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    with pytest.raises(ValueError) as excinfo:
        build_stt_provider("whisper", settings=Settings())
    message = str(excinfo.value)
    assert "Unknown STT provider 'whisper'" in message
    assert "first-party speech plugin" in message
    assert "pip install -e '.[voice]'" in message


def test_build_tts_unknown_first_party_plugin_gets_actionable_hint(monkeypatch):
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    with pytest.raises(ValueError) as excinfo:
        build_tts_provider("edge_tts", settings=Settings())
    message = str(excinfo.value)
    assert "Unknown TTS provider 'edge_tts'" in message
    assert "first-party speech plugin" in message
    assert "pip install -e '.[voice]'" in message


def test_build_truly_unknown_name_has_no_plugin_hint(monkeypatch):
    monkeypatch.setattr(discovery, "load_entry_point_factories", lambda group: {})
    with pytest.raises(ValueError) as excinfo:
        build_stt_provider("missing", settings=Settings())
    assert "first-party" not in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Whisper STT plugin
# ---------------------------------------------------------------------------


class _FakeWhisperModel:
    def __init__(self) -> None:
        self.transcribed: bytes | None = None
        self.transcribed_path: str | None = None

    def transcribe(self, path: str, language=None) -> dict:
        self.transcribed = Path(path).read_bytes()
        self.transcribed_path = path
        return {"text": "hello from whisper"}


class _FakeWhisperModule:
    def __init__(self, model) -> None:
        self._model = model
        self.loaded: list[str] = []

    def load_model(self, name: str):
        self.loaded.append(name)
        return self._model


def test_whisper_provider_transcribes(monkeypatch):
    fake_model = _FakeWhisperModel()
    fake_module = _FakeWhisperModule(fake_model)
    monkeypatch.setitem(sys.modules, "whisper", fake_module)

    provider = WhisperSTTProvider(model_name="base")
    assert fake_module.loaded == ["base"]

    audio = b"\x00\x01\x02\x03"
    text = asyncio.run(provider.transcribe(audio))
    assert text == "hello from whisper"
    assert fake_model.transcribed == audio
    # The temporary audio file handed to whisper is cleaned up afterwards.
    assert fake_model.transcribed_path is not None
    assert not Path(fake_model.transcribed_path).exists()


def test_whisper_build_factory_uses_config(monkeypatch):
    fake_module = _FakeWhisperModule(_FakeWhisperModel())
    monkeypatch.setitem(sys.modules, "whisper", fake_module)
    provider = build_whisper_stt(Settings(BERU_VOICE_STT_MODEL="large"))
    assert provider._model_name == "large"


# ---------------------------------------------------------------------------
# Edge TTS plugin
# ---------------------------------------------------------------------------


class _FakeCommunicate:
    def __init__(self, text: str, voice: str) -> None:
        self.text = text
        self.voice = voice

    async def stream(self):
        yield {"type": "audio", "data": b"ID3"}
        yield {"type": "WordBoundary", "data": {"offset": 0}}
        yield {"type": "audio", "data": b"-tail"}


class _FakeEdgeTTsModule:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def Communicate(self, text: str, voice: str) -> _FakeCommunicate:
        self.calls.append((text, voice))
        return _FakeCommunicate(text, voice)


def test_edge_tts_provider_synthesizes(monkeypatch):
    fake_module = _FakeEdgeTTsModule()
    monkeypatch.setitem(sys.modules, "edge_tts", fake_module)

    provider = EdgeTTSProvider(voice="en-US-JennyNeural")
    assert provider.format == "mp3"
    audio = asyncio.run(provider.synthesize("hello there"))
    assert audio == b"ID3-tail"  # non-audio frames are skipped
    assert fake_module.calls == [("hello there", "en-US-JennyNeural")]


def test_edge_tts_build_factory_uses_config(monkeypatch):
    fake_module = _FakeEdgeTTsModule()
    monkeypatch.setitem(sys.modules, "edge_tts", fake_module)
    provider = build_edge_tts(Settings(BERU_VOICE_TTS_VOICE="en-GB-SoniaNeural"))
    assert provider._voice == "en-GB-SoniaNeural"


# ---------------------------------------------------------------------------
# Format-aware audio clips
# ---------------------------------------------------------------------------


def test_new_clip_mp3_reports_mp3_duration():
    spec = AudioSpec()
    clip = new_clip("x", b"\x00" * 6000, spec, format="mp3")  # 6 KB @ 48 kbps = 1 s
    payload = clip.to_dict()
    assert payload["format"] == "mp3"
    assert payload["duration_ms"] == 1000.0


def test_new_clip_wav_duration_is_exact():
    spec = AudioSpec(sample_rate=16_000, channels=1, sample_width=2)
    clip = new_clip("x", b"\x00" * (16_000 * 2), spec)  # 1 s of 16-bit mono PCM
    assert clip.to_dict()["duration_ms"] == 1000.0


async def test_engine_respond_uses_provider_format():
    class Mp3TTS:
        format = "mp3"

        async def synthesize(self, text: str, **kwargs) -> bytes:
            return b"\xff\xfb" + b"\x00" * 24

    engine = VoiceEngine(tts=Mp3TTS())
    session = await engine.create_session()
    clip = await engine.respond(session.session_id, "hi")
    assert clip.format == "mp3"
    assert clip.audio.startswith(b"\xff\xfb")
    assert clip.to_dict()["format"] == "mp3"


# ---------------------------------------------------------------------------
# Packaging metadata
# ---------------------------------------------------------------------------


def test_pyproject_declares_build_system_and_entry_points():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    build_system = data["build-system"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    assert any("setuptools" in req for req in build_system["requires"])

    assert data["project"]["version"] == __version__
    dependency_names = {dep.split(">=")[0].lower() for dep in data["project"]["dependencies"]}
    for required in ("fastapi", "sqlalchemy", "asyncpg", "aiosqlite", "alembic"):
        assert required in dependency_names

    assert data["project"]["optional-dependencies"]["voice"]

    entry_points = data["project"]["entry-points"]
    assert entry_points["beru.stt_providers"]["whisper"] == (
        "backend.engines.speech.whisper:build_whisper_stt"
    )
    assert entry_points["beru.tts_providers"]["edge_tts"] == (
        "backend.engines.speech.edge_tts:build_edge_tts"
    )

    packages_find = data["tool"]["setuptools"]["packages"]["find"]
    assert "backend*" in packages_find["include"]
    assert "migrations*" in packages_find["include"]


def test_plugin_fixture_declares_tool_entry_point():
    data = tomllib.loads((PLUGIN_FIXTURE / "pyproject.toml").read_text(encoding="utf-8"))
    tool_group = data["project"]["entry-points"]["beru.tools"]
    assert "beru_sample_tool:GreetingTool" in tool_group.values()


def test_built_wheel_ships_backend_migrations_and_entry_points(tmp_path):
    """Build a bare wheel offline and verify the package layout + metadata."""
    src = tmp_path / "src"
    wheels = tmp_path / "wheels"
    src.mkdir()
    wheels.mkdir()
    shutil.copy2(PYPROJECT, src / "pyproject.toml")
    shutil.copytree(ROOT / "backend", src / "backend")
    shutil.copytree(ROOT / "migrations", src / "migrations")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheels),
            ".",
        ],
        cwd=str(src),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheel = next(wheels.glob("beru-*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        assert "backend/engines/speech/whisper.py" in names
        assert "backend/engines/speech/edge_tts.py" in names
        assert "backend/plugins/discovery.py" in names
        assert "migrations/env.py" in names
        assert "migrations/script.py.mako" in names
        entry_points_txt = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        assert len(entry_points_txt) == 1
        entry_points = zf.read(entry_points_txt[0]).decode()
        assert "[beru.stt_providers]" in entry_points
        assert "[beru.tts_providers]" in entry_points
        assert "whisper = backend.engines.speech.whisper:build_whisper_stt" in entry_points