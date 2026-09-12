"""Tests for the core tools: web search, file analysis, document generation, calendar.

Real backends are not implemented yet for file_analyser, document_generator,
and calendar; these tools report themselves as unavailable and never fabricate
results.
"""

from __future__ import annotations

from backend.agents.registry import get_agent_registry
from backend.tools.calendar import CalendarTool
from backend.tools.document import FileAnalyserTool
from backend.tools.generation import DocumentGeneratorTool
from backend.tools.web import WebSearchTool

# ---- WebSearchTool tests ----

# Real web search is not wired up yet, so web_search reports "unavailable"
# rather than fabricating results.


async def test_web_search_reports_unavailable():
    tool = WebSearchTool()
    result = await tool.run(query="python testing")
    assert result.ok is False
    assert "unavailable" in result.error


async def test_web_search_permissions():
    tool = WebSearchTool()
    assert "read" in tool.permissions


# ---- FileAnalyserTool tests ----


async def test_file_analyser_reports_unavailable():
    tool = FileAnalyserTool()
    result = await tool.run(path="/tmp/test.py", analysis_type="summary")
    assert result.ok is False
    assert "unavailable" in result.error
    assert "no file analysis backend" in result.error
    assert result.output is None


async def test_file_analyser_permissions():
    tool = FileAnalyserTool()
    assert "read" in tool.permissions


# ---- DocumentGeneratorTool tests ----

# Real document generation needs an LLM backend, which is not implemented yet:
# the tool reports itself as unavailable rather than fabricating a document.


async def test_document_generator_reports_unavailable():
    tool = DocumentGeneratorTool()
    result = await tool.run(prompt="Write a summary", format="markdown")
    assert result.ok is False
    assert result.error
    assert result.output is None


async def test_document_generator_needs_llm_credential():
    tool = DocumentGeneratorTool()
    assert tool.required_credentials == ["llm_api_key"]
    # Without an injected llm_api_key the tool fails honestly, naming the
    # missing credential — it never emits a fake document body.
    result = await tool.run(prompt="Write something")
    assert result.ok is False
    assert "llm_api_key" in result.error


async def test_document_generator_permissions():
    tool = DocumentGeneratorTool()
    assert "write" in tool.permissions


# ---- CalendarTool tests ----


async def test_calendar_reports_unavailable():
    tool = CalendarTool()
    result = await tool.run(
        action="create_event", title="Meeting", date="2025-06-01T10:00:00Z"
    )
    assert result.ok is False
    assert "unavailable" in result.error
    assert "not implemented" in result.error


async def test_calendar_unknown_action():
    tool = CalendarTool()
    result = await tool.run(action="delete_all")
    assert result.ok is False
    assert "Unknown calendar action" in result.error


async def test_calendar_permissions():
    tool = CalendarTool()
    assert "read" in tool.permissions
    assert "write" in tool.permissions


# ---- Core agent registration tests ----


async def test_core_agent_has_all_core_tools():
    registry = get_agent_registry()
    core = registry.get("beru_core")
    tool_names = [t.name for t in core._tools.values()]

    assert "clock" in tool_names
    assert "web_search" in tool_names
    assert "file_analyser" in tool_names
    assert "document_generator" in tool_names
    assert "calendar" in tool_names
