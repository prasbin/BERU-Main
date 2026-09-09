"""Tests for real LLM tool calling integration.

Verifies that:
1. Tool definitions are correctly serialized and sent to the LLM provider
2. Tool calls from the LLM are correctly parsed
3. Message serialization includes tool_calls and tool_call_id
4. Unavailable tools are filtered out of LLM-facing definitions
5. The end-to-end tool calling loop works with a real provider
6. Confirmation-required tools gate behind user approval
7. Error handling covers all failure modes
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from backend.agents.base import AgentRequest, BaseAgent
from backend.core.config import Settings
from backend.core.errors import LLMProviderError
from backend.engines.llm.base import (
    LLMMessage,
    ToolCall,
    ToolDefinition,
)
from backend.engines.llm.mock import MockProvider
from backend.engines.llm.openai_compatible import (
    OpenAICompatibleProvider,
    _parse_tool_calls,
    _serialize_messages,
    _serialize_tool_definitions,
)
from backend.tools.base import Tool, ToolResult
from backend.tools.clock import ClockTool
from backend.tools.policy import PermissionPolicy
from backend.tools.system import RunCommandTool


class FakeEchoTool(Tool):
    name = "echo_tool"
    description = "Echoes input back."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"echo": kwargs.get("text", "")})


class FakeFailTool(Tool):
    name = "fail_tool"
    description = "Always fails."
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure("intentional failure")


class FakeConfirmTool(Tool):
    name = "confirm_tool"
    description = "Requires confirmation."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"executed": True, "action": kwargs.get("action")})


class FakeUnavailableTool(Tool):
    name = "unavailable_tool"
    description = "This tool is unavailable."
    availability = "unavailable"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success("should never be called")


class FakeLimitedTool(Tool):
    name = "limited_tool"
    description = "This tool is limited."
    availability = "limited"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"status": "limited but callable"})


class BrokenTool(Tool):
    name = "broken"
    description = "Breaks"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("something broke")


#: Permissions granted to "test_agent" in these mechanics tests. The global
#: default policy is default-deny (execute only for beru_core/igris), so the
#: run/confirm/deny tests below grant the tags explicitly.
_EXEC_POLICY = PermissionPolicy(
    global_permissions=frozenset({"read", "read_clock", "write", "execute", "notify"})
)


def _agent_with_tools(*tools: Tool) -> BaseAgent:
    agent = BaseAgent(policy=_EXEC_POLICY)
    agent.name = "test_agent"
    for t in tools:
        agent.register_tool(t)
    return agent


def _make_provider(
    base_url: str = "http://fake-llm/v1", model: str = "test-model"
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(base_url=base_url, model=model, api_key="test-key")


def _make_openai_response(
    content: str | None = "Hello!",
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
    usage: dict | None = None,
) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "cmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _sse_lines(lines: list[str]) -> str:
    return "\n".join(lines) + "\ndata: [DONE]\n"


def _mock_httpx_response(data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=data)


def _mock_httpx_sse(lines: list[str]) -> httpx.Response:
    body = _sse_lines(lines)
    return httpx.Response(status_code=200, text=body, headers={"content-type": "text/event-stream"})


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self._calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._calls.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(500, json={"error": "no more mock responses"})


# ---- WS4: Unavailable tools filtered from LLM definitions ----


def test_tool_definitions_exclude_unavailable():
    agent = _agent_with_tools(FakeEchoTool(), FakeUnavailableTool(), FakeLimitedTool())
    defs = agent.tool_definitions
    names = [d.name for d in defs]
    assert "echo_tool" in names
    assert "limited_tool" in names
    assert "unavailable_tool" not in names


def test_tool_definitions_all_available():
    agent = _agent_with_tools(FakeEchoTool(), FakeLimitedTool())
    defs = agent.tool_definitions
    assert len(defs) == 2


def test_tool_definitions_empty_when_all_unavailable():
    agent = _agent_with_tools(FakeUnavailableTool())
    defs = agent.tool_definitions
    assert len(defs) == 0


def test_list_tools_includes_all_regardless_of_availability():
    agent = _agent_with_tools(FakeEchoTool(), FakeUnavailableTool())
    tools = agent.list_tools()
    names = [t.name for t in tools]
    assert "echo_tool" in names
    assert "unavailable_tool" in names


# ---- Serialization helpers ----


def test_serialize_tool_definitions():
    defs = [
        ToolDefinition(
            name="get_time", description="Get current time",
            parameters={"type": "object"},
        ),
        ToolDefinition(
            name="run_cmd", description="Run a command",
            parameters={
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
            },
        ),
    ]
    result = _serialize_tool_definitions(defs)
    assert len(result) == 2
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "get_time"
    assert result[0]["function"]["description"] == "Get current time"
    assert result[1]["function"]["name"] == "run_cmd"
    assert "cmd" in result[1]["function"]["parameters"]["properties"]


def test_serialize_tool_definitions_empty():
    assert _serialize_tool_definitions([]) == []


def test_serialize_messages_basic():
    msgs = [
        LLMMessage(role="system", content="You are helpful."),
        LLMMessage(role="user", content="Hello"),
        LLMMessage(role="assistant", content="Hi there!"),
    ]
    result = _serialize_messages(msgs)
    assert result[0] == {"role": "system", "content": "You are helpful."}
    assert result[1] == {"role": "user", "content": "Hello"}
    assert result[2] == {"role": "assistant", "content": "Hi there!"}


def test_serialize_messages_with_tool_calls():
    msgs = [
        LLMMessage(role="user", content="What time is it?"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="call_1", name="clock", arguments="{}"),
                ToolCall(id="call_2", name="echo_tool", arguments='{"text": "hi"}'),
            ],
        ),
    ]
    result = _serialize_messages(msgs)
    assert result[1]["role"] == "assistant"
    assert len(result[1]["tool_calls"]) == 2
    assert result[1]["tool_calls"][0]["id"] == "call_1"
    assert result[1]["tool_calls"][0]["type"] == "function"
    assert result[1]["tool_calls"][0]["function"]["name"] == "clock"
    assert result[1]["tool_calls"][1]["function"]["arguments"] == '{"text": "hi"}'


def test_serialize_messages_with_tool_result():
    msgs = [
        LLMMessage(role="assistant", content="", tool_calls=[
            ToolCall(id="call_1", name="clock", arguments="{}"),
        ]),
        LLMMessage(
            role="tool",
            content='{"result": {"utc": "2026-08-31T12:00:00"}}',
            tool_call_id="call_1",
        ),
    ]
    result = _serialize_messages(msgs)
    assert result[1]["role"] == "tool"
    assert result[1]["tool_call_id"] == "call_1"
    assert "utc" in result[1]["content"]


def test_serialize_messages_empty_tool_calls():
    msg = LLMMessage(role="assistant", content="Hello", tool_calls=[])
    result = _serialize_messages([msg])
    assert "tool_calls" not in result[0]


def test_serialize_messages_no_tool_call_id():
    msg = LLMMessage(role="tool", content="result", tool_call_id=None)
    result = _serialize_messages([msg])
    assert "tool_call_id" not in result[0]


def test_parse_tool_calls_none():
    assert _parse_tool_calls(None) == []


def test_parse_tool_calls_empty():
    assert _parse_tool_calls([]) == []


def test_parse_tool_calls_valid():
    raw = [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "clock", "arguments": "{}"},
        },
        {
            "id": "call_def456",
            "type": "function",
            "function": {"name": "echo_tool", "arguments": '{"text": "hello"}'},
        },
    ]
    result = _parse_tool_calls(raw)
    assert len(result) == 2
    assert result[0].id == "call_abc123"
    assert result[0].name == "clock"
    assert result[0].arguments == "{}"
    assert result[1].name == "echo_tool"
    assert result[1].arguments == '{"text": "hello"}'


def test_parse_tool_calls_skips_empty_name():
    raw = [
        {"id": "call_1", "type": "function", "function": {"name": "clock", "arguments": "{}"}},
        {"id": "call_2", "type": "function", "function": {"name": "", "arguments": "{}"}},
        {"id": "call_3", "type": "function", "function": {}},
    ]
    result = _parse_tool_calls(raw)
    assert len(result) == 1
    assert result[0].name == "clock"


def test_parse_tool_calls_with_missing_function():
    raw = [{"id": "call_1", "type": "function"}]
    result = _parse_tool_calls(raw)
    assert len(result) == 0


# ---- WS1: OpenAI provider sends tools and parses tool_calls ----


@pytest.mark.asyncio
async def test_chat_includes_tools_in_payload():
    transport = FakeTransport([_mock_httpx_response(_make_openai_response())])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")

    tools = [ToolDefinition(name="clock", description="Get time", parameters={})]
    messages = [LLMMessage(role="user", content="What time is it?")]
    await provider.chat(messages, tools=tools)

    body = json.loads(transport._calls[0].content)
    assert "tools" in body
    assert len(body["tools"]) == 1
    assert body["tools"][0]["function"]["name"] == "clock"
    await provider.aclose()


@pytest.mark.asyncio
async def test_chat_no_tools_when_none():
    transport = FakeTransport([_mock_httpx_response(_make_openai_response())])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")

    messages = [LLMMessage(role="user", content="Hi")]
    response = await provider.chat(messages)

    body = json.loads(transport._calls[0].content)
    assert "tools" not in body
    assert response.content == "Hello!"
    await provider.aclose()


@pytest.mark.asyncio
async def test_chat_parses_tool_calls_from_response():
    tool_calls_raw = [
        {"id": "call_abc", "type": "function", "function": {"name": "clock", "arguments": "{}"}},
    ]
    resp_data = _make_openai_response(
        content=None, tool_calls=tool_calls_raw,
        finish_reason="tool_calls",
    )
    transport = FakeTransport([_mock_httpx_response(resp_data)])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")

    tools = [ToolDefinition(name="clock", description="Get time", parameters={})]
    messages = [LLMMessage(role="user", content="What time is it?")]
    response = await provider.chat(messages, tools=tools)

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "clock"
    assert response.tool_calls[0].id == "call_abc"
    assert response.tool_calls[0].arguments == "{}"
    assert response.finish_reason == "tool_calls"
    await provider.aclose()


@pytest.mark.asyncio
async def test_chat_serializes_tool_result_messages():
    transport = FakeTransport([_mock_httpx_response(_make_openai_response())])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")

    tools = [ToolDefinition(name="clock", description="Get time", parameters={})]
    messages = [
        LLMMessage(role="user", content="What time is it?"),
        LLMMessage(role="assistant", content="", tool_calls=[
            ToolCall(id="call_1", name="clock", arguments="{}"),
        ]),
        LLMMessage(role="tool", content='{"result": {"utc": "2026"}}', tool_call_id="call_1"),
    ]
    await provider.chat(messages, tools=tools)

    body = json.loads(transport._calls[0].content)
    msg_tc = body["messages"][1]
    assert msg_tc["tool_calls"][0]["id"] == "call_1"
    assert msg_tc["tool_calls"][0]["function"]["name"] == "clock"

    msg_result = body["messages"][2]
    assert msg_result["role"] == "tool"
    assert msg_result["tool_call_id"] == "call_1"
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_chat_includes_tools():
    sse = [
        'data: {"id":"cmpl-1","model":"test-model",'
        '"choices":[{"index":0,"delta":{"content":"Hi"},'
        '"finish_reason":null}]}',
        'data: {"id":"cmpl-1","model":"test-model",'
        '"choices":[{"index":0,"delta":{},'
        '"finish_reason":"stop"}]}',
    ]
    transport = FakeTransport([_mock_httpx_sse(sse)])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")

    tools = [ToolDefinition(name="clock", description="Get time", parameters={})]
    messages = [LLMMessage(role="user", content="Hi")]
    _ = [c async for c in provider.stream_chat(messages, tools=tools)]

    body = json.loads(transport._calls[0].content)
    assert "tools" in body
    assert body["tools"][0]["function"]["name"] == "clock"
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_chat_parses_tool_call_fragments():
    sse = [
        'data: {"id":"cmpl-1","model":"test-model",'
        '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"id":"call_x","type":"function",'
        '"function":{"name":"clock","arguments":""}}]},'
        '"finish_reason":null}]}',
        'data: {"id":"cmpl-1","model":"test-model",'
        '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"id":"","type":"function",'
        '"function":{"name":"","arguments":"{}"}}]},'
        '"finish_reason":null}]}',
        'data: {"id":"cmpl-1","model":"test-model",'
        '"choices":[{"index":0,"delta":{},'
        '"finish_reason":"tool_calls"}]}',
    ]
    transport = FakeTransport([_mock_httpx_sse(sse)])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")

    tools = [ToolDefinition(name="clock", description="Get time", parameters={})]
    messages = [LLMMessage(role="user", content="What time?")]
    chunks = [c async for c in provider.stream_chat(messages, tools=tools)]

    done_chunk = [c for c in chunks if c.done]
    assert len(done_chunk) == 1
    tc = done_chunk[0].tool_calls
    assert len(tc) == 1
    assert tc[0].name == "clock"
    assert tc[0].arguments == "{}"
    await provider.aclose()


# ---- WS2-3: End-to-end tool calling loop ----


async def test_agent_tool_loop_clock():
    agent = _agent_with_tools(ClockTool(), FakeEchoTool())
    provider = MockProvider()
    request = AgentRequest(
        user_message="What time is it right now?",
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )
    result = await agent.generate(request)
    assert result.content is not None
    assert result.tool_calls_made >= 1


async def test_agent_tool_loop_echo():
    agent = _agent_with_tools(FakeEchoTool())
    provider = MockProvider()
    request = AgentRequest(
        user_message="echo hello world",
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )
    result = await agent.generate(request)
    assert result.content is not None


async def test_agent_tool_loop_confirmation_required():
    agent = _agent_with_tools(FakeConfirmTool())
    provider = MockProvider()
    request = AgentRequest(
        user_message="do the confirm action",
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )
    result = await agent.generate(request)
    assert len(result.pending_confirmations) >= 1
    assert result.pending_confirmations[0].tool_name == "confirm_tool"


async def test_agent_tool_loop_tool_failure():
    agent = _agent_with_tools(FakeFailTool())
    provider = MockProvider()
    request = AgentRequest(
        user_message="use the fail tool",
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )
    result = await agent.generate(request)
    assert result.tool_calls_made >= 1
    assert result.content is not None


# ---- WS3: Confirmation flow end-to-end ----


async def test_confirm_tool_call_executes():
    agent = _agent_with_tools(FakeConfirmTool())
    provider = MockProvider()
    request = AgentRequest(
        user_message="do something with confirm_tool",
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )
    result = await agent.generate(request)
    assert len(result.pending_confirmations) >= 1

    pending = result.pending_confirmations[0]
    # The mock provider synthesises generic arguments; give the confirm tool
    # valid ones so the re-invocation exercises the confirmation gate only.
    pending.arguments = {"action": "do"}
    tool_call = ToolCall(
        id=pending.tool_call_id,
        name=pending.tool_name,
        arguments=json.dumps(pending.arguments),
    )
    exec_result = await agent.run_tool(tool_call, confirm=True, agent_name="test_agent")
    assert exec_result.ok is True
    assert exec_result.output["executed"] is True


async def test_deny_tool_call_does_not_execute():
    agent = _agent_with_tools(FakeConfirmTool())
    provider = MockProvider()
    request = AgentRequest(
        user_message="do something with confirm_tool",
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )
    result = await agent.generate(request)
    assert len(result.pending_confirmations) >= 1

    pending = result.pending_confirmations[0]
    tool_call = ToolCall(
        id=pending.tool_call_id,
        name=pending.tool_name,
        arguments=json.dumps(pending.arguments),
    )
    exec_result = await agent.run_tool(tool_call, confirm=False, agent_name="test_agent")
    assert exec_result.ok is False
    assert exec_result.output == {"confirmation_required": True}


# ---- WS5: Error handling ----


@pytest.mark.asyncio
async def test_provider_timeout_raises_error():
    transport = FakeTransport([])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")
    provider._max_retries = 0

    with patch.object(provider._client, "post", side_effect=httpx.ReadTimeout("timeout")):
        with pytest.raises(LLMProviderError):
            await provider.chat([LLMMessage(role="user", content="Hi")])
    await provider.aclose()


@pytest.mark.asyncio
async def test_provider_http_500_raises_error():
    transport = FakeTransport([_mock_httpx_response({"error": "server error"}, status_code=500)])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")
    provider._max_retries = 0

    with pytest.raises(LLMProviderError):
        await provider.chat([LLMMessage(role="user", content="Hi")])
    await provider.aclose()


@pytest.mark.asyncio
async def test_provider_401_raises_error():
    transport = FakeTransport([_mock_httpx_response({"error": "unauthorized"}, status_code=401)])
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://fake-llm/v1")
    provider._max_retries = 0

    with pytest.raises(LLMProviderError):
        await provider.chat([LLMMessage(role="user", content="Hi")])
    await provider.aclose()


async def test_invalid_tool_name_returns_error():
    agent = _agent_with_tools(FakeEchoTool())
    tc = ToolCall(id="call_1", name="nonexistent_tool", arguments="{}")
    result = await agent.run_tool(tc, confirm=True, agent_name="test")
    assert result.ok is False
    assert "Unknown tool" in (result.error or "")


async def test_invalid_json_arguments_returns_error():
    agent = _agent_with_tools(FakeEchoTool())
    tc = ToolCall(id="call_1", name="echo_tool", arguments="not-json")
    result = await agent.run_tool(tc, confirm=True, agent_name="test")
    assert result.ok is False
    assert "Invalid JSON" in (result.error or "")


async def test_tool_execution_exception_becomes_error():
    agent = _agent_with_tools(BrokenTool())
    tc = ToolCall(id="call_1", name="broken", arguments="{}")
    result = await agent.run_tool(tc, confirm=True, agent_name="test")
    assert result.ok is False
    assert "something broke" in (result.error or "")


# ---- Real host tools ----


async def test_run_command_executes():
    agent = _agent_with_tools(RunCommandTool())
    tc = ToolCall(
        id="call_1", name="run_command",
        arguments=json.dumps({"command": "echo test123"}),
    )
    result = await agent.run_tool(tc, confirm=True, agent_name="test")
    assert result.ok is True
    assert result.output["status"] == "completed"
    assert "test123" in result.output["stdout"]


async def test_run_command_needs_confirmation():
    agent = _agent_with_tools(RunCommandTool())
    tc = ToolCall(id="call_1", name="run_command", arguments=json.dumps({"command": "echo nope"}))
    result = await agent.run_tool(tc, confirm=False, agent_name="test")
    assert result.ok is False
    assert result.output == {"confirmation_required": True}


async def test_get_system_info_executes():
    agent = _agent_with_tools(ClockTool())
    tc = ToolCall(id="call_1", name="clock", arguments="{}")
    result = await agent.run_tool(tc, confirm=True, agent_name="test")
    assert result.ok is True
    assert "utc" in result.output


# ---- Streaming end-to-end ----


async def test_agent_stream_tool_loop():
    agent = _agent_with_tools(ClockTool())
    provider = MockProvider()
    request = AgentRequest(
        user_message="What time is it?",
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )
    chunks = [c async for c in agent.stream(request)]
    assert len(chunks) > 0
    done_chunks = [c for c in chunks if c.done]
    assert len(done_chunks) >= 1
    assert done_chunks[0].agent == "test_agent"
