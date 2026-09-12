"""End-to-end agent tool-calling through the real runtime.

These tests drive the complete pipeline — HTTP ``/api/v1/chat`` -> ChatService ->
IntelligenceEngine -> shared agent registry -> real tool execution -> result fed
back to the model -> reply persisted — using the same shared, per-test cached
registry the app serves. They complement the unit-level coverage in
``test_tool_calling.py`` (fabricated tools) and ``test_agent_real_tools.py``
(real tools at the agent layer) by going over the full stack and asserting the
real registry's agents actually execute their tools and the results flow back.

The deterministic ``MockProvider`` simulates a tool call against the selected
agent's first non-unavailable tool (``tools[0]``) with ``{"query": "mock"}``,
then replies on the following iteration with the mock echo text; ``tools[0]``
therefore runs for real inside every loop here. Each test gets a freshly
seeded registry (the autouse fixture clears the cache), so registering a custom
agent does not leak between tests.
"""

from __future__ import annotations

import json
from typing import Any

from backend.agents.base import AgentRequest, BaseAgent
from backend.agents.registry import get_agent_registry
from backend.core.config import Settings, get_settings
from backend.engines.llm.base import (
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
from backend.tools.base import Tool, ToolResult


class RecordCallTool(Tool):
    """A real, non-gated tool that records invocations and returns a fixed value.

    Accepts ``**kwargs`` (the mock calls it with ``{"query": "mock"}``) and
    records every call so a test can prove the tool actually ran through the
    full stack.
    """

    name = "record_probe"
    description = "Records how many times it was called."
    permissions = ["read"]
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0
        self.last_args: dict[str, Any] = {}

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        self.last_args = dict(kwargs)
        return ToolResult.success({"calls": self.calls, "value": "probe-result"})


class GroundingProvider(LLMProvider):
    """Returns one tool call, then replies echoing the tool result it saw.

    The second invocation inspects the ``tool``-role message the agent fed
    back and echoes its text, proving the real tool output reached the model.
    """

    name = "mock_grounding"

    def __init__(self, tool_name: str):
        self._tool_name = tool_name
        self.seen_tool_result = ""

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
        if tools and not any(m.role == "tool" for m in messages):
            return LLMResponse(
                content="",
                model="mock",
                tool_calls=[
                    ToolCall(
                        id="call_e2e_1",
                        name=self._tool_name,
                        arguments=json.dumps({"query": "mock"}),
                    )
                ],
                finish_reason="tool_calls",
            )
        tool_result = next(
            (m.content for m in reversed(list(messages)) if m.role == "tool"),
            "",
        )
        self.seen_tool_result = tool_result
        return LLMResponse(
            content="model saw: " + (tool_result or ""),
            model="mock",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None


def _make_request(provider, message: str = "hello e2e") -> AgentRequest:
    return AgentRequest(
        user_message=message,
        history=[],
        provider=provider,
        settings=Settings(llm_provider="mock"),
    )


def _register_e2e_agent(name: str, tool: Tool) -> BaseAgent:
    agent = BaseAgent()
    agent.name = name
    agent.register_tool(tool)
    get_agent_registry().register(agent, replace=True)
    return agent


# ---- Full HTTP stack: POST /api/v1/chat ----

async def test_http_chat_executes_real_registry_tool(client):
    """POST /api/v1/chat runs a real tool from the shared registry for real."""
    probe = RecordCallTool()
    _register_e2e_agent("e2e_probe", probe)

    resp = await client.post(
        "/api/v1/chat",
        json={"message": "check the probe", "agent": "e2e_probe"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent"] == "e2e_probe"
    assert body["message"]["role"] == "assistant"
    assert "check the probe" in body["message"]["content"]

    # The real tool ran through the full HTTP -> service -> engine -> registry
    # stack, and the mock's {"query": "mock"} call reached it.
    assert probe.calls == 1
    assert probe.last_args == {"query": "mock"}


async def test_http_chat_executes_real_core_clock_tool(client):
    """The default beru_core agent's first tool (the real clock) runs over HTTP."""
    resp = await client.post("/api/v1/chat", json={"message": "what time is it"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent"] == "beru_core"
    assert body["conversation_id"]
    # The mock echoes the user message after running tools[0] and feeding the
    # result back, so the whole turn completed atomically.
    assert "what time is it" in body["message"]["content"]


async def test_http_chat_persists_completed_tool_turn(client):
    """After an HTTP chat with a tool call, exactly user+assistant are stored."""
    resp = await client.post("/api/v1/chat", json={"message": "tool turn e2e"})
    assert resp.status_code == 200
    conversation_id = resp.json()["conversation_id"]

    messages = (await client.get(f"/api/v1/conversations/{conversation_id}/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"], "assistant reply must be persisted"


# ---- Full HTTP stack: POST /api/v1/chat/stream (SSE) ----

async def test_stream_chat_executes_tool_via_registry(client):
    """The streaming path also executes registry tools and returns a reply."""
    probe = RecordCallTool()
    _register_e2e_agent("e2e_stream", probe)

    text = ""
    async with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "stream with tools", "agent": "e2e_stream"},
    ) as resp:
        assert resp.status_code == 200
        async for chunk in resp.aiter_text():
            text += chunk

    assert "e2e_stream" in text or "stream with tools" in text
    # The streamed turn executed the real tool once before replying.
    assert probe.calls == 1


# ---- Service + engine through the real registry (grounding) ----

async def test_tool_result_is_fed_back_to_model_via_engine():
    """The real tool result reaches the model's next turn."""
    probe = RecordCallTool()
    agent = _register_e2e_agent("e2e_ground", probe)

    provider = GroundingProvider("record_probe")
    result = await agent.generate(_make_request(provider, message="use the probe"))

    assert result.tool_calls_made == 1
    # GroundingProvider echoes the serialized tool result the agent fed back;
    # "probe-result" proves the real tool output reached the model.
    assert "probe-result" in result.content
    assert "probe-result" in provider.seen_tool_result
    assert '"calls": 1' in provider.seen_tool_result


async def test_e2e_core_agent_runs_its_tools_directly():
    """Engine + shared beru_core registry run a real tool (30-tool agent)."""
    from backend.engines.intelligence import get_intelligence_engine

    settings = get_settings()
    engine = get_intelligence_engine()
    result = await engine.generate(
        agent_name="beru_core",
        history=[],
        user_message="query the model using a tool",
        settings=settings,
    )
    assert result.agent == "beru_core"
    assert result.tool_calls_made == 1
    assert "BERU (mock)" in result.content


# ---- Durable credential scoping with a real tool ----

# The single real tool wired to the Stage 5.4 scoping mechanism is
# document_generator (legitimate need: real generation requires an LLM key).
# These tests drive that real, registry-backed tool through the agent runtime
# to prove the scoped view is granted, unrelated/owner keys are not exposed,
# and a missing required credential fails honestly.


def _core_doc_generator():
    from backend.agents.registry import get_agent_registry

    core = get_agent_registry().get("beru_core")
    return core, core._tools["document_generator"]


async def test_e2e_document_generator_receives_only_its_scoped_credential():
    """Authorized run: the tool gets exactly llm_api_key, nothing else."""
    core, tool = _core_doc_generator()
    assert tool.required_credentials == ["llm_api_key"]

    call = ToolCall(
        id="call_cred_1",
        name="document_generator",
        arguments=json.dumps({"prompt": "write a report", "format": "markdown"}),
    )
    settings = Settings(
        llm_api_key="sk-e2e-llm",
        BERU_EMBEDDING_API_KEY="sk-e2e-emb",
        BERU_API_KEY="owner-secret",
    )
    result = await core.run_tool(call, confirm=True, agent_name="beru_core", settings=settings)

    # Authorized credential is available to the tool...
    assert tool.credentials.get("llm_api_key") == "sk-e2e-llm"
    # ...while unrelated and owner credentials are never exposed.
    assert tool.credentials.has("embedding_api_key") is False
    assert "api_key" not in tool.credentials.names()
    assert "owner-secret" not in repr(tool.credentials)
    assert "sk-e2e-emb" not in repr(tool.credentials)
    assert "sk-e2e-llm" not in repr(tool.credentials)

    # The generation backend is not implemented, so even with the key the tool
    # fails honestly rather than fabricating a document.
    assert result.ok is False
    assert "not implemented" in (result.error or "")


async def test_e2e_document_generator_missing_key_fails_honestly():
    """Without the declared credential the tool names it and fails, no fake output."""
    core, tool = _core_doc_generator()
    call = ToolCall(
        id="call_cred_2",
        name="document_generator",
        arguments=json.dumps({"prompt": "anything"}),
    )
    result = await core.run_tool(call, confirm=True, agent_name="beru_core", settings=None)

    assert tool.credentials.empty()
    assert result.ok is False
    assert "llm_api_key" in (result.error or "")
    assert result.output is None