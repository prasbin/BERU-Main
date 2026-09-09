"""Tests for the tool-calling loop in agents."""

from __future__ import annotations

import json
from typing import Any

from backend.agents.base import AgentRequest, BaseAgent
from backend.core.config import Settings
from backend.engines.llm.base import (
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
from backend.tools.base import Tool, ToolResult

# ---- Test tools ----


class EchoTool(Tool):
    name = "echo"
    description = "Echoes the input back."
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success(kwargs.get("text", ""))


class FailTool(Tool):
    name = "fail"
    description = "Always fails."
    permissions = ["read"]
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure("intentional failure")


class CountingTool(Tool):
    name = "counter"
    description = "Counts invocations."
    permissions = ["read"]
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.call_count = 0

    async def run(self, **kwargs: Any) -> ToolResult:
        self.call_count += 1
        return ToolResult.success({"count": self.call_count})


# ---- Mock providers for tool-calling tests ----


class ToolCallThenTextProvider(LLMProvider):
    """Returns a tool call on the first invocation, then text on the second."""

    name = "mock_tool"

    def __init__(self, tool_name: str = "echo", tool_args: str = '{"text": "hello"}'):
        self._tool_name = tool_name
        self._tool_args = tool_args
        self._call_count = 0

    async def chat(
        self,
        messages,
        *,
        model=None,
        temperature=None,
        max_tokens=None,
        tools=None,
        **kwargs,
    ) -> LLMResponse:
        self._call_count += 1
        if self._call_count == 1 and tools:
            return LLMResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name=self._tool_name,
                        arguments=self._tool_args,
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="Final answer after tool use.",
            model="mock",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None


class MultiToolCallProvider(LLMProvider):
    """Returns multiple tool calls, then text."""

    name = "mock_multi"

    def __init__(self, num_iterations: int = 2):
        self._num = num_iterations
        self._call_count = 0

    async def chat(
        self,
        messages,
        *,
        model=None,
        temperature=None,
        max_tokens=None,
        tools=None,
        **kwargs,
    ) -> LLMResponse:
        self._call_count += 1
        if self._call_count <= self._num and tools:
            return LLMResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(
                        id=f"call_{self._call_count}",
                        name="echo",
                        arguments=json.dumps({"text": f"iter_{self._call_count}"}),
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="Done.",
            model="mock",
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None


class AlwaysToolCallProvider(LLMProvider):
    """Always returns a tool call (to test max iteration limit)."""

    name = "mock_infinite"

    def __init__(self):
        self._call_count = 0

    async def chat(
        self,
        messages,
        *,
        model=None,
        temperature=None,
        max_tokens=None,
        tools=None,
        **kwargs,
    ) -> LLMResponse:
        self._call_count += 1
        return LLMResponse(
            content="",
            model="mock",
            tool_calls=[
                ToolCall(
                    id=f"call_{self._call_count}",
                    name="echo",
                    arguments='{"text": "loop"}',
                )
            ],
            finish_reason="tool_calls",
        )

    async def aclose(self) -> None:
        return None


# ---- Tests ----


def _make_request(provider: LLMProvider, message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_message=message,
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )


async def test_tool_call_basic():
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(EchoTool())

    provider = ToolCallThenTextProvider()
    result = await agent.generate(_make_request(provider))

    assert result.content == "Final answer after tool use."
    assert result.tool_calls_made == 1
    assert result.model == "mock"


async def test_tool_call_result_fed_to_llm():
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(EchoTool())

    provider = ToolCallThenTextProvider("echo", '{"text": "hello"}')
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 1


async def test_multiple_tool_iterations():
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(EchoTool())

    provider = MultiToolCallProvider(num_iterations=3)
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 3
    assert result.content == "Done."


async def test_max_iterations_limit():
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(EchoTool())

    provider = AlwaysToolCallProvider()
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 10
    assert "maximum" in result.content.lower()


async def test_unknown_tool_call():
    """When the LLM calls a tool the agent doesn't have, it gets an error message."""
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(EchoTool())  # register at least one tool so tools are sent

    class OneCallProvider(LLMProvider):
        name = "mock"

        def __init__(self):
            self._called = False

        async def chat(
            self, messages, *, model=None, temperature=None, max_tokens=None, tools=None, **kwargs
        ):
            if not self._called and tools:
                self._called = True
                return LLMResponse(
                    content="",
                    model="mock",
                    tool_calls=[ToolCall(id="c1", name="nonexistent", arguments="{}")],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="Done.", model="mock", finish_reason="stop")

    provider = OneCallProvider()
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 1
    assert result.content == "Done."


async def test_no_tools_registered():
    agent = BaseAgent()
    agent.name = "test"

    provider = ToolCallThenTextProvider()
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 0


async def test_tool_definitions_format():
    agent = BaseAgent()
    agent.register_tool(EchoTool())

    defs = agent.tool_definitions
    assert len(defs) == 1
    assert defs[0].name == "echo"
    assert defs[0].description == "Echoes the input back."
    assert "text" in defs[0].parameters["properties"]


async def test_execute_tool_failure():
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(FailTool())

    tc = ToolCall(id="c1", name="fail", arguments="{}")
    result = await agent._execute_tool(tc)
    parsed = json.loads(result)
    assert "error" in parsed


async def test_execute_tool_bad_json():
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(EchoTool())

    tc = ToolCall(id="c1", name="echo", arguments="not-json")
    result = await agent._execute_tool(tc)
    parsed = json.loads(result)
    assert "error" in parsed


async def test_tool_call_accumulates_usage():
    agent = BaseAgent()
    agent.name = "test"
    agent.register_tool(EchoTool())

    provider = ToolCallThenTextProvider()
    result = await agent.generate(_make_request(provider))

    assert result.usage is not None
    assert result.usage.total_tokens > 0
