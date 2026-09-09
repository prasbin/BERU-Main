"""Tests for the agent and tool subsystems."""

from __future__ import annotations

import pytest

from backend.agents.base import AgentRequest
from backend.agents.core_agent import CoreAgent
from backend.agents.registry import get_agent_registry
from backend.core.config import get_settings
from backend.core.errors import NotFoundError
from backend.engines.llm.mock import MockProvider
from backend.tools.registry import get_tool_registry


def test_registry_resolves_default_agent():
    registry = get_agent_registry()
    assert registry.get(None).name == "beru_core"


def test_registry_unknown_agent_raises_not_found():
    registry = get_agent_registry()
    with pytest.raises(NotFoundError):
        registry.get("no_such_agent")


async def test_core_agent_generates_reply():
    agent = CoreAgent()
    request = AgentRequest(
        user_message="Ping",
        history=[],
        provider=MockProvider(),
        settings=get_settings(),
    )
    result = await agent.generate(request)
    assert result.agent == "beru_core"
    assert "Ping" in result.content
    assert result.usage is not None


def test_tool_registry_has_clock():
    tool = get_tool_registry().get("clock")
    assert tool.name == "clock"
    assert "read_clock" in tool.permissions


async def test_clock_tool_runs():
    tool = get_tool_registry().get("clock")
    result = await tool.run()
    assert result.ok is True
    assert "utc" in result.output
