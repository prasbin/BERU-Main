"""Tests for the activity ledger and approval audit trail (reliability pass).

Covers the module directly, its API surface, and the two integration points
that write into it: tool execution (``run_tool``) and the confirm/deny flow.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import BaseAgent, ToolCall
from backend.services.activity_ledger import (
    ActivityEntry,
    AuditEntry,
    args_summary,
    error_summary,
    get_activity_ledger,
    reset_activity_ledger,
    truncate,
)
from backend.services.chat_service import ChatService


@pytest.fixture
def ledger():
    reset_activity_ledger()
    yield get_activity_ledger()
    reset_activity_ledger()


@pytest.fixture
def clock_client(client):
    """Wrap the app client with a fresh ledger per test."""
    reset_activity_ledger()
    yield client
    reset_activity_ledger()


def test_truncate_bounds():
    assert truncate("short", 20) == "short"
    assert len(truncate("x" * 500, 200)) <= 200
    assert truncate("x" * 500, 200).endswith("...")


def test_args_error_summary():
    assert args_summary({"a": 1}) == "{'a': 1}"
    assert len(args_summary({"big": "z" * 500})) <= 200
    assert error_summary(None) is None
    assert len(error_summary("e" * 500)) <= 200


def test_ledger_ring_bounds(ledger):
    for i in range(600):
        ledger.record_activity(ActivityEntry(
            timestamp=i, request_id="r", agent="beru_core",
            tool_name="clock", args_summary="{}", outcome="success",
            duration_ms=1.0, error=None,
        ))
    entries = ledger.list_activity(limit=10**6)
    assert len(entries) <= 500
    assert entries[0]["timestamp"] < entries[-1]["timestamp"]


def test_ledger_summary_counts(ledger):
    ledger.record_activity(ActivityEntry(
        timestamp=1, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    ledger.record_activity(ActivityEntry(
        timestamp=2, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    ledger.record_activity(ActivityEntry(
        timestamp=3, request_id="r", agent="beru_core", tool_name="run_command",
        args_summary="{}", outcome="failure", duration_ms=2.0, error="boom",
    ))
    ledger.record_activity(ActivityEntry(
        timestamp=4, request_id="r", agent="beru_core", tool_name="run_command",
        args_summary="{}", outcome="confirmation_required", duration_ms=None,
        error=None,
    ))
    ledger.record_audit(AuditEntry(
        timestamp=5, request_id="r", agent="beru_core", tool_name="run_command",
        decision="approved", confirmation_id="c1", outcome="success", error=None,
    ))
    ledger.record_audit(AuditEntry(
        timestamp=6, request_id="r", agent="beru_core", tool_name="launch_app",
        decision="denied", confirmation_id="c2", outcome=None, error=None,
    ))
    s = ledger.summary()
    assert s["total_tool_calls"] == 4
    assert s["successes"] == 2
    assert s["failures"] == 1
    assert s["confirmations_pending"] == 1
    assert s["approvals"] == 1
    assert s["denials"] == 1
    assert s["top_tools"][0] == ("clock", 2)


async def test_run_tool_records_activity(ledger):
    agent = BaseAgent()
    from backend.tools.clock import ClockTool

    agent.register_tool(ClockTool())
    tool_call = ToolCall(id="t1", name="clock", arguments="{}")
    result = await agent.run_tool(tool_call, agent_name="beru_core")
    assert result.ok is True
    entries = ledger.list_activity()
    assert len(entries) == 1
    assert entries[0]["tool_name"] == "clock"
    assert entries[0]["outcome"] == "success"
    assert entries[0]["agent"] == "beru_core"
    assert entries[0]["duration_ms"] is not None


async def test_run_tool_records_failure(ledger):
    agent = BaseAgent()
    from backend.tools.clock import ClockTool

    class BoomTool(ClockTool):
        name = "boom"

        async def run(self, **kwargs):
            raise ValueError("kaboom")

    agent.register_tool(BoomTool())
    tool_call = ToolCall(id="t1", name="boom", arguments="{}")
    result = await agent.run_tool(tool_call, agent_name="beru_core")
    assert result.ok is False
    entries = ledger.list_activity()
    assert entries[0]["outcome"] == "failure"
    assert "kaboom" in entries[0]["error"]


async def test_run_tool_records_confirmation_request(ledger):
    agent = BaseAgent()
    from backend.tools.system import RunCommandTool

    agent.register_tool(RunCommandTool())
    tool_call = ToolCall(id="t1", name="run_command", arguments='{"command": "whoami"}')
    result = await agent.run_tool(tool_call, agent_name="beru_core")
    assert result.ok is False
    assert result.output.get("confirmation_required") is True
    entries = ledger.list_activity()
    assert entries[0]["outcome"] == "confirmation_required"


async def test_reliability_summary_endpoint(clock_client):
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    resp = await clock_client.get("/api/v1/reliability/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["total_tool_calls"] == 1


async def test_reliability_activity_endpoint(clock_client):
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1, request_id="rid-1", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    resp = await clock_client.get("/api/v1/reliability/activity")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert entries[-1]["tool_name"] == "clock"
    assert entries[-1]["request_id"] == "rid-1"


async def test_reliability_clear_endpoint(clock_client):
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    resp = await clock_client.delete("/api/v1/reliability/activity")
    assert resp.status_code == 200
    assert resp.json()["cleared"] is True
    assert ledger.list_activity() == []


async def test_status_includes_reliability(client):
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "reliability" in body
    assert "total_tool_calls" in body["reliability"]


async def test_deny_tool_call_records_audit(ledger, db_session: AsyncSession):
    from backend.engines.intelligence import get_intelligence_engine
    from backend.memory.conversation_memory import ConversationMemory
    from backend.services.confirmation_service import ConfirmationService
    from backend.services.conversation_service import ConversationService

    service = ChatService(
        engine=get_intelligence_engine(),
        conversation_service=ConversationService(),
        memory=ConversationMemory(),
        confirmations=ConfirmationService(),
    )
    record = service._confirmations.create(
        conversation_id="conv_ledger",
        agent="beru_core",
        tool_name="run_command",
        arguments={"command": "true"},
        tool_call_id="call_ledger",
    )
    service.deny_tool_call("conv_ledger", record.id)
    denials = [
        e for e in ledger.list_audit(limit=10**9)
        if e["decision"] == "denied" and e["tool_name"] == "run_command"
    ]
    assert len(denials) == 1
    assert denials[0]["confirmation_id"] == record.id
    assert service._confirmations.get(record.id) is None
    assert len(ledger.list_activity()) == 0

