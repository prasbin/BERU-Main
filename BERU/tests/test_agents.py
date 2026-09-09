"""Tests for the specialist agents: IGRIS, DHANUS, TANK."""

from __future__ import annotations

import json

from backend.agents.base import AgentRequest
from backend.agents.registry import get_agent_registry
from backend.core.config import Settings
from backend.engines.llm.base import (
    LLMProvider,
    LLMResponse,
    ToolCall,
)

# ---- Mock providers ----


class EchoToolCallProvider(LLMProvider):
    """Returns a tool call for the first tool, then text."""

    name = "mock"

    def __init__(self, tool_name: str = "clock", args: str = "{}"):
        self._tool_name = tool_name
        self._args = args
        self._called = False

    async def chat(
        self, messages, *, model=None, temperature=None, max_tokens=None, tools=None, **kwargs
    ):
        if not self._called and tools:
            self._called = True
            return LLMResponse(
                content="",
                model="mock",
                tool_calls=[ToolCall(id="c1", name=self._tool_name, arguments=self._args)],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="Done.", model="mock", finish_reason="stop")

    async def aclose(self):
        return None


def _make_request(provider, message="test"):
    return AgentRequest(
        user_message=message,
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )


# ---- Agent registration tests ----


def test_registry_has_all_agents():
    registry = get_agent_registry()
    agents = registry.list()
    names = [a.name for a in agents]
    assert "beru_core" in names
    assert "igris" in names
    assert "dhanus" in names
    assert "tank" in names


def test_registry_get_by_name():
    registry = get_agent_registry()
    assert registry.get("igris").name == "igris"
    assert registry.get("dhanus").name == "dhanus"
    assert registry.get("tank").name == "tank"
    assert registry.get(None).name == "beru_core"


# ---- IGRIS agent tests ----


def test_igris_metadata():
    from backend.agents.igris_agent import IgrisAgent

    agent = IgrisAgent()
    assert agent.name == "igris"
    assert "study_assistance" in agent.capabilities
    assert "research" in agent.capabilities
    assert "IGRIS" in agent.default_system_prompt


async def test_igris_has_tools():
    registry = get_agent_registry()
    igris = registry.get("igris")
    tool_names = [t.name for t in igris._tools.values()]
    assert "search_knowledge" in tool_names
    assert "create_flashcard" in tool_names


async def test_igris_search_knowledge():
    registry = get_agent_registry()
    igris = registry.get("igris")
    tc = ToolCall(id="c1", name="search_knowledge", arguments='{"query": "quantum physics"}')
    result_text = await igris._execute_tool(tc, agent_name="igris")
    result = json.loads(result_text)

    assert result["result"]["query"] == "quantum physics"
    assert result["result"]["count"] == 1


async def test_igris_create_flashcard():
    registry = get_agent_registry()
    igris = registry.get("igris")
    tc = ToolCall(
        id="c1",
        name="create_flashcard",
        arguments='{"front": "What is NP?", "back": "Nondeterministic Polynomial time"}',
    )
    result_text = await igris._execute_tool(tc, agent_name="igris")
    result = json.loads(result_text)

    assert result["result"]["flashcard_created"] is True
    assert result["result"]["front"] == "What is NP?"


async def test_igris_tool_call_loop():
    registry = get_agent_registry()
    igris = registry.get("igris")
    tool = igris._tools.get("search_knowledge")
    saved = tool.availability if tool else None
    if tool:
        tool.availability = "available"
    try:
        provider = EchoToolCallProvider("search_knowledge", '{"query": "test"}')
        result = await igris.generate(_make_request(provider))
        assert result.tool_calls_made == 1
        assert result.content == "Done."
    finally:
        if tool and saved is not None:
            tool.availability = saved


# ---- DHANUS agent tests ----


def test_dhanus_metadata():
    from backend.agents.dhanus_agent import DhanusAgent

    agent = DhanusAgent()
    assert agent.name == "dhanus"
    assert "spiritual_guidance" in agent.capabilities
    assert "meditation" in agent.capabilities
    assert "DHANUS" in agent.default_system_prompt


async def test_dhanus_has_tools():
    registry = get_agent_registry()
    dhanus = registry.get("dhanus")
    tool_names = [t.name for t in dhanus._tools.values()]
    assert "lookup_scripture" in tool_names
    assert "meditation_timer" in tool_names


async def test_dhanus_lookup_scripture():
    registry = get_agent_registry()
    dhanus = registry.get("dhanus")
    tc = ToolCall(
        id="c1",
        name="lookup_scripture",
        arguments='{"tradition": "vedantic", "topic": "self"}',
    )
    result_text = await dhanus._execute_tool(tc, agent_name="dhanus")
    result = json.loads(result_text)

    assert result["result"]["tradition"] == "vedantic"
    assert result["result"]["topic"] == "self"


async def test_dhanus_meditation_timer():
    registry = get_agent_registry()
    dhanus = registry.get("dhanus")
    tc = ToolCall(
        id="c1",
        name="meditation_timer",
        arguments='{"duration_minutes": 15, "technique": "breathing"}',
    )
    result_text = await dhanus._execute_tool(tc, agent_name="dhanus")
    result = json.loads(result_text)

    assert result["result"]["timer_set"] is True
    assert result["result"]["duration_minutes"] == 15


# ---- TANK agent tests ----


def test_tank_metadata():
    from backend.agents.tank_agent import TankAgent

    agent = TankAgent()
    assert agent.name == "tank"
    assert "programming" in agent.capabilities
    assert "debugging" in agent.capabilities
    assert "TANK" in agent.default_system_prompt


async def test_tank_has_tools():
    registry = get_agent_registry()
    tank = registry.get("tank")
    tool_names = [t.name for t in tank._tools.values()]
    assert "code_analyser" in tool_names
    assert "code_formatter" in tool_names


async def test_tank_code_analyser():
    registry = get_agent_registry()
    tank = registry.get("tank")
    tc = ToolCall(
        id="c1",
        name="code_analyser",
        arguments='{"code": "def foo(): pass", "language": "python"}',
    )
    result_text = await tank._execute_tool(tc, agent_name="tank")
    result = json.loads(result_text)

    assert result["result"]["language"] == "python"
    assert result["result"]["line_count"] == 1


async def test_tank_code_formatter():
    registry = get_agent_registry()
    tank = registry.get("tank")
    tc = ToolCall(
        id="c1",
        name="code_formatter",
        arguments='{"code": "x=1", "language": "python"}',
    )
    result_text = await tank._execute_tool(tc, agent_name="tank")
    result = json.loads(result_text)

    assert result["result"]["formatted_code"] == "x=1"
    assert result["result"]["language"] == "python"


# ---- Cross-agent isolation tests ----


async def test_igris_cannot_use_tank_tools():
    """IGRIS agent should not have TANK's tools registered."""
    registry = get_agent_registry()
    igris = registry.get("igris")
    tool_names = [t.name for t in igris._tools.values()]

    assert "code_analyser" not in tool_names
    assert "code_formatter" not in tool_names


async def test_tank_cannot_use_igris_tools():
    """TANK agent should not have IGRIS's tools registered."""
    registry = get_agent_registry()
    tank = registry.get("tank")
    tool_names = [t.name for t in tank._tools.values()]

    assert "search_knowledge" not in tool_names
    assert "create_flashcard" not in tool_names


async def test_dhanus_cannot_use_core_tools():
    """DHANUS agent should not have the clock tool."""
    registry = get_agent_registry()
    dhanus = registry.get("dhanus")
    tool_names = [t.name for t in dhanus._tools.values()]

    assert "clock" not in tool_names
