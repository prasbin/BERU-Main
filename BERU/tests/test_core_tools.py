"""Tests for the core tools: web search, file analysis, document generation, calendar.

Real backends are exercised directly against the tool layer: file_analyser
reads real files, calendar persists to a JSON store injected via tmp_path,
and document_generator fails honestly with the offline mock provider or
succeeds when wired to a stub LLM that returns genuine text.
"""

from __future__ import annotations

import textwrap

from backend.agents.registry import get_agent_registry
from backend.core.config import Settings
from backend.engines.llm.base import LLMProvider, LLMResponse, LLMUsage
from backend.tools.calendar import CalendarTool
from backend.tools.document import FileAnalyserTool
from backend.tools.generation import DocumentGeneratorTool
from backend.tools.web import WebSearchTool

# --------------------------------------------------------------------------- #
# Stub providers (test-only; never talks to the network)
# --------------------------------------------------------------------------- #

class _EchoProvider(LLMProvider):
    name = "echo"

    async def chat(
        self, messages, *, model=None, temperature=None, max_tokens=None, tools=None, **kwargs
    ):
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(
            content=f"echo-doc: {last}",
            model="echo-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
        )


# --------------------------------------------------------------------------- #
# WebSearchTool (unchanged: still no real backend)
# --------------------------------------------------------------------------- #


async def test_web_search_reports_unavailable():
    tool = WebSearchTool()
    result = await tool.run(query="python testing")
    assert result.ok is False
    assert "unavailable" in result.error


async def test_web_search_permissions():
    tool = WebSearchTool()
    assert "read" in tool.permissions


# --------------------------------------------------------------------------- #
# FileAnalyserTool
# --------------------------------------------------------------------------- #


async def test_file_analyser_returns_real_stats(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(textwrap.dedent("""\
        def greet(name):
            print(f"hello {name}")

        x = 1
        y = 2
    """), encoding="utf-8")
    tool = FileAnalyserTool()
    result = await tool.run(path=str(sample), analysis_type="stats")
    assert result.ok is True
    out = result.output
    assert out["line_count"] == 5
    assert out["nonblank_line_count"] == 4
    assert out["language"] == "python"
    assert out["exists"] is True
    assert out["size_bytes"] == sample.stat().st_size
    assert out["truncated"] is False


async def test_file_analyser_summary(tmp_path):
    sample = tmp_path / "note.txt"
    lines = [f"line {i}" for i in range(10)]
    sample.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tool = FileAnalyserTool()
    result = await tool.run(path=str(sample), analysis_type="summary")
    assert result.ok is True
    assert "content" in result.output
    assert result.output["line_count"] == 10
    assert result.output["lines_shown"] == 10


async def test_file_analyser_structure(tmp_path):
    sample = tmp_path / "code.py"
    sample.write_text("# header\n\ndef foo(): pass\nclass Bar:\n    pass\n", encoding="utf-8")
    tool = FileAnalyserTool()
    result = await tool.run(path=str(sample), analysis_type="structure")
    assert result.ok is True
    markers = result.output["markers"]
    assert any("header" in m["text"] for m in markers)
    assert any("def foo" in m["text"] for m in markers)
    assert any("class Bar" in m["text"] for m in markers)
    assert result.output["marker_count"] == 3


async def test_file_analyser_missing_file():
    result = await FileAnalyserTool().run(path="/nonexistent/file.txt")
    assert result.ok is False
    assert "No such file" in result.error


async def test_file_analyser_unknown_analysis_type(tmp_path):
    sample = tmp_path / "x.txt"
    sample.write_text("a")
    result = await FileAnalyserTool().run(path=str(sample), analysis_type="boop")
    assert result.ok is False
    assert "Unknown analysis_type" in result.error


async def test_file_analyser_permissions():
    assert "read" in FileAnalyserTool().permissions


# --------------------------------------------------------------------------- #
# DocumentGeneratorTool
# --------------------------------------------------------------------------- #


async def test_document_generator_needs_llm_credential():
    tool = DocumentGeneratorTool()
    assert tool.required_credentials == ["llm_api_key"]
    result = await tool.run(prompt="Write something")
    assert result.ok is False
    assert "llm_api_key" in result.error


async def test_document_generator_honest_with_mock_provider():
    from backend.tools.credentials import scope_for_tool

    tool = DocumentGeneratorTool()
    tool.settings = Settings(llm_provider="mock", llm_api_key="dummy")
    tool.credentials = scope_for_tool(
        Settings(llm_provider="mock", llm_api_key="dummy"), tool.required_credentials
    )
    result = await tool.run(prompt="Write a summary", format="markdown")
    assert result.ok is False
    assert "mock" in result.error.lower()


async def test_document_generator_success_with_real_provider():
    """When a real provider is wired, the tool returns genuine LLM content."""
    from backend.engines.llm.registry import BUILTIN_FACTORIES
    from backend.tools.credentials import scope_for_tool

    saved = BUILTIN_FACTORIES.get("echo")
    BUILTIN_FACTORIES["echo"] = lambda settings: _EchoProvider()
    try:
        tool = DocumentGeneratorTool()
        tool.settings = Settings(llm_provider="echo", llm_api_key="dummy")
        tool.credentials = scope_for_tool(
            Settings(llm_provider="echo", llm_api_key="dummy"), tool.required_credentials
        )
        result = await tool.run(prompt="Write about testing", format="markdown", title="Tests")
    finally:
        if saved is None:
            del BUILTIN_FACTORIES["echo"]
        else:
            BUILTIN_FACTORIES["echo"] = saved

    assert result.ok is True
    assert "echo-doc:" in result.output["content"]
    assert result.output["title"] == "Tests"
    assert result.output["format"] == "markdown"
    assert result.output["model"] == "echo-model"
    assert result.output["word_count"] >= 2


async def test_document_generator_empty_body_is_honest():
    """If the provider returns empty content the tool says so."""
    from backend.tools.credentials import scope_for_tool

    class _EmptyProvider(LLMProvider):
        name = "empty"

        async def chat(
            self, messages, *, model=None, temperature=None, max_tokens=None, tools=None, **kwargs
        ):
            return LLMResponse(content="", model="empty", finish_reason="stop", raw={})

    from backend.engines.llm.registry import BUILTIN_FACTORIES

    saved = BUILTIN_FACTORIES.get("empty")
    BUILTIN_FACTORIES["empty"] = lambda settings: _EmptyProvider()
    try:
        tool = DocumentGeneratorTool()
        tool.settings = Settings(llm_provider="empty", llm_api_key="dummy")
        tool.credentials = scope_for_tool(
            Settings(llm_provider="empty", llm_api_key="dummy"), tool.required_credentials
        )
        result = await tool.run(prompt="Say nothing", format="text")
    finally:
        if saved is None:
            del BUILTIN_FACTORIES["empty"]
        else:
            BUILTIN_FACTORIES["empty"] = saved

    assert result.ok is False
    assert "empty" in result.error.lower()


async def test_document_generator_permissions():
    assert "write" in DocumentGeneratorTool().permissions


# --------------------------------------------------------------------------- #
# CalendarTool
# --------------------------------------------------------------------------- #


async def test_calendar_create_and_list(tmp_path):
    cal = CalendarTool(store_path=tmp_path / "cal.json")
    r1 = await cal.run(action="create_event", title="Standup", date="2026-09-13T10:00:00Z")
    assert r1.ok is True
    assert len(r1.output["id"]) == 12
    assert r1.output["status"] == "scheduled"

    r2 = await cal.run(action="create_task", title="Ship remediation", date="2026-09-14")
    assert r2.ok is True
    assert r2.output["status"] == "open"

    r3 = await cal.run(action="list_events")
    assert r3.ok is True
    assert r3.output["count"] == 2


async def test_calendar_complete_task(tmp_path):
    cal = CalendarTool(store_path=tmp_path / "cal.json")
    r = await cal.run(action="create_task", title="Deploy", date="2026-09-15")
    task_id = r.output["id"]
    r2 = await cal.run(action="complete_task", task_id=task_id)
    assert r2.ok is True
    assert r2.output["status"] == "done"


async def test_calendar_complete_nonexistent_task(tmp_path):
    cal = CalendarTool(store_path=tmp_path / "cal.json")
    result = await cal.run(action="complete_task", task_id="no-such-id")
    assert result.ok is False
    assert "no task" in result.error.lower()


async def test_calendar_list_filter_by_kind(tmp_path):
    cal = CalendarTool(store_path=tmp_path / "cal.json")
    await cal.run(action="create_event", title="ev", date="x")
    await cal.run(action="create_task", title="ta", date="x")
    evts = await cal.run(action="list_events", kind="event")
    assert evts.output["count"] == 1
    tasks = await cal.run(action="list_events", kind="task")
    assert tasks.output["count"] == 1


async def test_calendar_unknown_action(tmp_path):
    cal = CalendarTool(store_path=tmp_path / "cal.json")
    result = await cal.run(action="delete_all")
    assert result.ok is False
    assert "Unknown calendar action" in result.error


async def test_calendar_permissions():
    tool = CalendarTool()
    assert "read" in tool.permissions
    assert "write" in tool.permissions


# --------------------------------------------------------------------------- #
# Core agent registration
# --------------------------------------------------------------------------- #


async def test_core_agent_has_all_core_tools():
    registry = get_agent_registry()
    core = registry.get("beru_core")
    tool_names = [t.name for t in core._tools.values()]
    assert "clock" in tool_names
    assert "web_search" in tool_names
    assert "file_analyser" in tool_names
    assert "document_generator" in tool_names
    assert "calendar" in tool_names