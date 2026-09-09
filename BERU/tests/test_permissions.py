"""Tests for tool permission enforcement."""

from __future__ import annotations

import json
from typing import Any

from backend.agents.base import AgentRequest, BaseAgent
from backend.core.config import Settings
from backend.engines.llm.base import (
    LLMProvider,
    LLMResponse,
    ToolCall,
)
from backend.tools.base import Tool, ToolResult
from backend.tools.policy import PermissionPolicy

# ---- Test tools with various permission requirements ----


class ReadClockTool(Tool):
    name = "clock"
    description = "Returns the current time."
    permissions = ["read_clock"]
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"time": "2026-01-01T00:00:00Z"})


class WriteFileTool(Tool):
    name = "write_file"
    description = "Writes to a file."
    permissions = ["write_file"]
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    requires_confirmation = True

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"written": True})


class AdminTool(Tool):
    name = "admin_tool"
    description = "Admin operation."
    permissions = ["admin"]
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"admin": True})


class NoPermTool(Tool):
    name = "no_perm"
    description = "Tool with no permissions."
    permissions = []
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"ok": True})


class MultiPermTool(Tool):
    name = "multi_perm"
    description = "Requires multiple permissions."
    permissions = ["read", "write"]
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"ok": True})


# ---- Mock providers ----


class SingleToolCallProvider(LLMProvider):
    """Returns one tool call, then text."""

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


# ---- Permission policy tests ----


def test_policy_allows_matching_permission():
    policy = PermissionPolicy(global_permissions=frozenset({"read_clock"}))
    assert policy.allowed_for("agent", ["read_clock"])


def test_policy_denies_missing_permission():
    policy = PermissionPolicy(global_permissions=frozenset({"read_clock"}))
    assert not policy.allowed_for("agent", ["write_file"])


def test_policy_agent_specific():
    policy = PermissionPolicy(
        agent_permissions={"agent_a": frozenset({"admin"})}
    )
    assert policy.allowed_for("agent_a", ["admin"])
    assert not policy.allowed_for("agent_b", ["admin"])


def test_policy_missing_permissions():
    policy = PermissionPolicy(global_permissions=frozenset({"read"}))
    missing = policy.missing_for("agent", ["read", "write", "admin"])
    assert missing == ["write", "admin"]


def test_policy_empty_permissions_always_allowed():
    policy = PermissionPolicy()
    assert policy.allowed_for("agent", [])


def test_policy_global_plus_agent():
    policy = PermissionPolicy(
        global_permissions=frozenset({"read"}),
        agent_permissions={"agent_a": frozenset({"write"})},
    )
    assert policy.allowed_for("agent_a", ["read", "write"])
    assert not policy.allowed_for("agent_b", ["read", "write"])


# ---- Agent permission enforcement tests ----


async def test_tool_allowed_by_policy():
    policy = PermissionPolicy(global_permissions=frozenset({"read_clock"}))
    agent = BaseAgent(policy=policy)
    agent.name = "test"
    agent.register_tool(ReadClockTool())

    provider = SingleToolCallProvider("clock")
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 1


async def test_tool_denied_by_policy():
    policy = PermissionPolicy(global_permissions=frozenset())  # no permissions
    agent = BaseAgent(policy=policy)
    agent.name = "test"
    agent.register_tool(ReadClockTool())

    provider = SingleToolCallProvider("clock")
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 1
    # The tool was "called" but returned a permission denied error
    # The LLM then responded with text


async def test_confirmation_required():
    policy = PermissionPolicy(global_permissions=frozenset({"write_file"}))
    agent = BaseAgent(policy=policy)
    agent.name = "test"
    agent.register_tool(WriteFileTool())

    provider = SingleToolCallProvider("write_file", '{"path": "/tmp/test"}')
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 1


async def test_tool_no_permissions_always_allowed():
    policy = PermissionPolicy()  # empty policy
    agent = BaseAgent(policy=policy)
    agent.name = "test"
    agent.register_tool(NoPermTool())

    provider = SingleToolCallProvider("no_perm")
    result = await agent.generate(_make_request(provider))

    assert result.tool_calls_made == 1


async def test_execute_tool_permission_denied():
    policy = PermissionPolicy(global_permissions=frozenset())  # no permissions
    agent = BaseAgent(policy=policy)
    agent.name = "test"
    agent.register_tool(ReadClockTool())

    tc = ToolCall(id="c1", name="clock", arguments="{}")
    result_text = await agent._execute_tool(tc, agent_name="test")
    result = json.loads(result_text)

    assert "error" in result
    assert "Permission denied" in result["error"]


async def test_execute_tool_confirmation_required():
    policy = PermissionPolicy(global_permissions=frozenset({"write_file"}))
    agent = BaseAgent(policy=policy)
    agent.name = "test"
    agent.register_tool(WriteFileTool())

    tc = ToolCall(id="c1", name="write_file", arguments='{"path": "/tmp/test"}')
    result_text = await agent._execute_tool(tc, agent_name="test")
    result = json.loads(result_text)

    assert result.get("confirmation_required") is True


async def test_execute_tool_confirmation_bypassed():
    policy = PermissionPolicy(global_permissions=frozenset({"write_file"}))
    agent = BaseAgent(policy=policy)
    agent.name = "test"
    agent.register_tool(WriteFileTool())

    tc = ToolCall(id="c1", name="write_file", arguments='{"path": "/tmp/test"}')
    result_text = await agent._execute_tool(tc, agent_name="test", confirm=True)
    result = json.loads(result_text)

    assert result.get("result") == {"written": True}


async def test_multi_permission_tool():
    policy = PermissionPolicy(
        global_permissions=frozenset({"read"}),
        agent_permissions={"agent_a": frozenset({"write"})},
    )
    agent = BaseAgent(policy=policy)
    agent.name = "agent_a"
    agent.register_tool(MultiPermTool())

    tc = ToolCall(id="c1", name="multi_perm", arguments="{}")
    result_text = await agent._execute_tool(tc, agent_name="agent_a")
    result = json.loads(result_text)

    assert result.get("result") == {"ok": True}


async def test_multi_permission_tool_partial():
    policy = PermissionPolicy(global_permissions=frozenset({"read"}))
    agent = BaseAgent(policy=policy)
    agent.name = "agent_a"
    agent.register_tool(MultiPermTool())

    tc = ToolCall(id="c1", name="multi_perm", arguments="{}")
    result_text = await agent._execute_tool(tc, agent_name="agent_a")
    result = json.loads(result_text)

    assert "error" in result
    assert "write" in result["error"]


# ---- Default policy (default-deny) ----

# An unknown agent name picks up only the global grants; an explicit policy is
# needed for anything else. These tests pin that default-deny contract.


async def test_default_policy_grants_read_only_to_unknown_agents():
    from backend.tools.system import RunCommandTool

    agent = BaseAgent()  # no policy -> default_policy()
    agent.name = "tester"  # not beru_core / igris -> global grants only
    agent.register_tool(RunCommandTool())  # requires "execute"

    tc = ToolCall(id="c1", name="run_command", arguments='{"command": "echo hi"}')
    result = json.loads(await agent._execute_tool(tc, agent_name="tester"))

    assert "error" in result
    assert "Permission denied" in result["error"]


async def test_default_policy_allows_read_tools_for_unknown_agents():
    agent = BaseAgent()  # default policy grants global "read", "read_clock"
    agent.name = "tester"
    agent.register_tool(ReadClockTool())

    tc = ToolCall(id="c1", name="clock", arguments="{}")
    result = json.loads(await agent._execute_tool(tc, agent_name="tester"))

    assert result.get("result") == {"time": "2026-01-01T00:00:00Z"}


async def test_default_policy_grants_execute_to_core_agent():
    from backend.tools.system import RunCommandTool

    agent = BaseAgent()  # default policy grants beru_core the full set
    agent.name = "beru_core"
    agent.register_tool(RunCommandTool())

    tc = ToolCall(id="c1", name="run_command", arguments='{"command": "echo hi"}')
    result = json.loads(await agent._execute_tool(tc, agent_name="beru_core"))

    # execute is granted -> the tool asks for confirmation instead of denying.
    assert result.get("confirmation_required") is True
