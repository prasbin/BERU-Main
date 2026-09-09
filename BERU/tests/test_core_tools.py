"""Tests for the core tools: web search, file analysis, document generation, calendar."""

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


async def test_file_analyser_summary():
    tool = FileAnalyserTool()
    result = await tool.run(path="/tmp/test.py")
    assert result.ok is True
    assert result.output["path"] == "/tmp/test.py"
    assert result.output["analysis_type"] == "summary"


async def test_file_analyser_stats_mode():
    tool = FileAnalyserTool()
    result = await tool.run(path="/tmp/test.py", analysis_type="stats")
    assert result.output["analysis_type"] == "stats"


async def test_file_analyser_structure_mode():
    tool = FileAnalyserTool()
    result = await tool.run(path="/tmp/test.py", analysis_type="structure")
    assert result.output["analysis_type"] == "structure"


async def test_file_analyser_permissions():
    tool = FileAnalyserTool()
    assert "read" in tool.permissions


async def test_file_analyser_analysis_in_output():
    tool = FileAnalyserTool()
    result = await tool.run(path="/tmp/test.py")
    assert "analysis" in result.output


# ---- DocumentGeneratorTool tests ----


async def test_document_generator_markdown():
    tool = DocumentGeneratorTool()
    result = await tool.run(prompt="Write a summary", format="markdown")
    assert result.ok is True
    assert result.output["format"] == "markdown"
    assert result.output["content"].startswith("#")


async def test_document_generator_title():
    tool = DocumentGeneratorTool()
    result = await tool.run(prompt="Write a report", title="Test Report")
    assert result.output["title"] == "Test Report"
    assert "Test Report" in result.output["content"]


async def test_document_generator_permissions():
    tool = DocumentGeneratorTool()
    assert "write" in tool.permissions


async def test_document_generator_word_count():
    tool = DocumentGeneratorTool()
    result = await tool.run(prompt="Write something")
    assert result.output["word_count"] > 0


async def test_document_generator_default_format():
    tool = DocumentGeneratorTool()
    result = await tool.run(prompt="test")
    assert result.output["format"] == "markdown"


# ---- CalendarTool tests ----


async def test_calendar_create_event():
    tool = CalendarTool()
    result = await tool.run(
        action="create_event", title="Meeting", date="2025-06-01T10:00:00Z"
    )
    assert result.ok is True
    assert result.output["action"] == "create_event"
    assert result.output["event_created"] is True
    assert result.output["title"] == "Meeting"


async def test_calendar_list_events():
    tool = CalendarTool()
    result = await tool.run(action="list_events")
    assert result.ok is True
    assert result.output["count"] == 1
    assert len(result.output["events"]) == 1


async def test_calendar_complete_task():
    tool = CalendarTool()
    result = await tool.run(action="complete_task", task_id="task_1")
    assert result.ok is True
    assert result.output["completed"] is True
    assert result.output["task_id"] == "task_1"


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
