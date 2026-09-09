"""Tests for the agent's real tool loop and the confirmation flow.

The core agent now executes real system tools through the shared host engines:
permission policy first, confirmation gate second, then genuine execution with
the result fed back into the reasoning loop (buffered and streamed). This file
verifies that behaviour end-to-end, including the explicit confirmation flow
and its single-use confirmation store.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import (
    AgentRequest,
    BaseAgent,
    PendingConfirmation,
)
from backend.core.config import Settings, get_settings
from backend.core.errors import NotFoundError
from backend.engines.intelligence import get_intelligence_engine
from backend.engines.llm.base import ToolCall
from backend.engines.llm.mock import MockProvider
from backend.memory.conversation_memory import ConversationMemory
from backend.services.chat_service import ChatService
from backend.services.confirmation_service import ConfirmationService
from backend.services.conversation_service import ConversationService
from backend.tools.base import serialize_tool_result
from backend.tools.policy import PermissionPolicy
from backend.tools.system import GetSystemInfoTool, RunCommandTool


def _make_request(provider=None, message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_message=message,
        history=[],
        provider=provider or MockProvider(),
        settings=Settings(llm_provider="mock"),
    )


#: Permissions granted in these execution-mechanics tests. The global default
#: policy is default-deny; these tests assert the run/confirm/block behaviour of
#: the loop, so they grant the tags explicitly.
_EXEC_POLICY = PermissionPolicy(
    global_permissions=frozenset({"read", "read_clock", "write", "execute", "notify"})
)


def _tool_agent(policy=None) -> BaseAgent:
    agent = BaseAgent(policy=policy if policy is not None else _EXEC_POLICY)
    agent.name = "tester"
    agent.register_tool(RunCommandTool())
    return agent


# ---- run_tool: permission, confirmation, real execution ----


async def test_run_command_executes_for_real_with_confirmation():
    agent = _tool_agent()
    tc = ToolCall(id="call_1", name="run_command", arguments=json.dumps({"command": "echo real"}))
    result = await agent.run_tool(tc, confirm=True, agent_name="tester")
    assert result.ok is True
    assert result.output["status"] == "completed"
    assert result.output["stdout"].strip() == "real"
    assert serialize_tool_result(result, tool_name="run_command") == json.dumps(
        {"result": result.output}
    )


async def test_run_command_requires_confirmation_and_does_not_execute():
    from backend.engines.command import get_command_executor

    executor = get_command_executor()
    history_before = len(executor.get_history())

    agent = _tool_agent()
    tc = ToolCall(id="call_1", name="run_command", arguments=json.dumps({"command": "echo never"}))
    result = await agent.run_tool(tc, confirm=False, agent_name="tester")

    assert result.ok is False
    assert result.output == {"confirmation_required": True}
    assert len(executor.get_history()) == history_before  # never executed


async def test_run_command_permission_denied():
    agent = _tool_agent(policy=PermissionPolicy())  # nothing allowed
    tc = ToolCall(id="call_1", name="run_command", arguments=json.dumps({"command": "echo x"}))
    result = await agent.run_tool(tc, confirm=True, agent_name="tester")
    assert result.ok is False
    assert "Permission denied" in (result.error or "")


async def test_destructive_command_blocked_even_when_confirmed():
    agent = _tool_agent()
    tc = ToolCall(id="call_1", name="run_command", arguments=json.dumps({"command": "rm -rf /x"}))
    result = await agent.run_tool(tc, confirm=True, agent_name="tester")
    assert result.ok is False
    assert "blocked" in (result.error or "").lower()


async def test_execution_failure_maps_to_error_result():
    agent = _tool_agent()
    tc = ToolCall(
        id="call_1",
        name="run_command",
        arguments=json.dumps({"command": "definitely_not_a_command_zzz_123"}),
    )
    result = await agent.run_tool(tc, confirm=True, agent_name="tester")
    assert result.ok is False
    assert "failed" in (result.error or "").lower()


async def test_unknown_tool_call():
    agent = _tool_agent()
    tc = ToolCall(id="call_1", name="no_such_tool", arguments="{}")
    result = await agent.run_tool(tc, confirm=True, agent_name="tester")
    assert result.ok is False
    assert "Unknown tool" in (result.error or "")


async def test_invalid_json_arguments():
    agent = _tool_agent()
    tc = ToolCall(id="call_1", name="run_command", arguments="not-json")
    result = await agent.run_tool(tc, confirm=True, agent_name="tester")
    assert result.ok is False
    assert "Invalid JSON" in (result.error or "")


# ---- The tool loop (buffered generate) ----


async def test_generate_surfaces_pending_confirmation_from_real_tool():
    """The MockProvider simulates a tool call against run_command (tools[0]).

    run_command is confirmation-gated, so the loop must NOT execute it — it
    surfaces a PendingConfirmation for the caller and lets the model reply.
    """
    agent = _tool_agent()
    result = await agent.generate(_make_request(message="run the echo command"))

    assert result.tool_calls_made == 1
    assert len(result.pending_confirmations) == 1
    pending = result.pending_confirmations[0]
    assert isinstance(pending, PendingConfirmation)
    assert pending.tool_name == "run_command"
    assert pending.arguments == {"query": "mock"}
    assert pending.tool_call_id.startswith("call_mock_")
    # The model answered in text after being told confirmation was required.
    assert "received" in result.content


async def test_generate_feeds_tool_result_back_without_confirmation():
    """A non-gated real tool executes inside the loop and its result reaches the model."""
    agent = BaseAgent()
    agent.name = "tester"
    agent.register_tool(GetSystemInfoTool())

    result = await agent.generate(_make_request(message="what is this host?"))
    # MockProvider simulates get_system_info (tools[0]) with {"query": "mock"};
    # the tool ignores extra kwargs and returns real host info.
    assert result.tool_calls_made == 1
    assert result.pending_confirmations == []
    assert "received" in result.content


# ---- Streaming tool loop ----


async def test_stream_runs_tool_loop_and_reports_pending_confirmations():
    agent = _tool_agent()
    chunks = [chunk async for chunk in agent.stream(_make_request(message="echo please"))]

    done_chunks = [c for c in chunks if c.done]
    assert done_chunks, "stream must end with a terminal chunk"
    terminal = done_chunks[-1]
    assert terminal.tool_calls_made == 1
    assert len(terminal.pending_confirmations) == 1
    assert terminal.pending_confirmations[0].tool_name == "run_command"
    # The final text reply was streamed as deltas after the tool iteration.
    assert any(c.delta for c in chunks)


# ---- respond_after_tool / continue_after_tool ----


async def test_respond_after_tool_concludes_grounded_in_result():
    call_args = json.dumps({"command": "echo echo_hi"})
    engine_result = await _tool_agent().run_tool(
        ToolCall(id="call_1", name="run_command", arguments=call_args),
        confirm=True,
        agent_name="tester",
    )
    tool_text = serialize_tool_result(engine_result, tool_name="run_command")

    agent = _tool_agent()
    result = await agent.respond_after_tool(
        _make_request(message="echo echo_hi"),
        ToolCall(id="call_1", name="run_command", arguments=call_args),
        tool_text,
    )
    # Mock provider, no tools: echoes the recovered original user request.
    assert "echo_hi" in result.content
    assert result.tool_calls_made == 0


# ---- ConfirmationService semantics ----


def test_confirmation_service_single_use():
    store = ConfirmationService()
    record = store.create(
        conversation_id="conv_1",
        agent="beru_core",
        tool_name="run_command",
        arguments={"command": "echo hi"},
        tool_call_id="call_1",
    )
    consumed = store.consume("conv_1", record.id)
    assert consumed.id == record.id
    assert consumed.arguments == {"command": "echo hi"}
    assert store.get(record.id) is None


def test_confirmation_service_wrong_conversation_raises():
    store = ConfirmationService()
    record = store.create(
        conversation_id="conv_1",
        agent="beru_core",
        tool_name="run_command",
        arguments={"command": "echo hi"},
        tool_call_id="call_1",
    )
    with pytest.raises(NotFoundError):
        store.consume("conv_other", record.id)
    # Not consumed by the failed attempt.
    assert store.get(record.id) is not None


def test_confirmation_service_unknown_id_raises():
    store = ConfirmationService()
    with pytest.raises(NotFoundError):
        store.consume("conv_1", "nope")


def test_confirmation_service_expired_raises():
    from datetime import datetime, timedelta, timezone

    store = ConfirmationService(ttl_seconds=1.0)
    record = store.create(
        conversation_id="conv_1",
        agent="beru_core",
        tool_name="run_command",
        arguments={},
        tool_call_id="call_1",
    )
    record.created_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with pytest.raises(NotFoundError):
        store.consume("conv_1", record.id)


# ---- Confirmation flow through the ChatService (real tool execution) ----


async def test_confirm_tool_call_executes_and_persists(db_session: AsyncSession):
    settings = get_settings()
    service = ChatService(
        engine=get_intelligence_engine(),
        conversation_service=ConversationService(),
        memory=ConversationMemory(),
        confirmations=ConfirmationService(),
    )
    conversations = service._conversations
    conversation = await conversations.create(db_session, agent="beru_core")
    await conversations.add_message(
        db_session, conversation, role="user", content="run echo goodbye"
    )

    record = service._confirmations.create(
        conversation_id=conversation.id,
        agent="beru_core",
        tool_name="run_command",
        arguments={"command": "echo goodbye"},
        tool_call_id="call_conf1",
    )
    outcome = await service.confirm_tool_call(
        db_session,
        settings,
        conversation_id=conversation.id,
        confirmation_id=record.id,
    )

    assert outcome.conversation.id == conversation.id
    assert outcome.result.agent == "beru_core"
    # The reply is grounded in the actual tool result and the original request.
    assert "goodbye" in outcome.assistant_message.content
    # The stored command really ran (shared executor history).
    from backend.engines.command import get_command_executor

    assert any("echo goodbye" in r.command for r in get_command_executor().get_history(limit=50))
    # Single-use: the record is gone.
    assert service._confirmations.get(record.id) is None

    # The approval was recorded in the reliability audit trail.
    from backend.services.activity_ledger import get_activity_ledger

    approvals = [
        e for e in get_activity_ledger().list_audit(limit=10**9)
        if e["decision"] == "approved" and e["tool_name"] == "run_command"
    ]
    assert approvals

    # A second approval of the same id cannot re-run the command.
    with pytest.raises(NotFoundError):
        await service.confirm_tool_call(
            db_session,
            settings,
            conversation_id=conversation.id,
            confirmation_id=record.id,
        )


async def test_chat_confirm_endpoint(client, _sessionmaker):
    from backend.services.confirmation_service import get_confirmation_service

    store = get_confirmation_service()
    store.clear()
    try:
        # Seed a conversation with a user message in the same temp DB the app uses.
        async with _sessionmaker() as session:
            conversations = ConversationService()
            conversation = await conversations.create(session, agent="beru_core")
            await conversations.add_message(
                session, conversation, role="user", content="run echo endpoint_confirm"
            )
            await session.commit()
            cid = conversation.id

        record = store.create(
            conversation_id=cid,
            agent="beru_core",
            tool_name="run_command",
            arguments={"command": "echo endpoint_confirm"},
            tool_call_id="call_e1",
        )

        resp = await client.post(
            "/api/v1/chat/confirm",
            json={"conversation_id": cid, "confirmation_id": record.id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent"] == "beru_core"
        assert body["conversation_id"] == cid
        assert "endpoint_confirm" in body["message"]["content"]

        # Single-use — re-approving the same confirmation is a clean 404.
        resp2 = await client.post(
            "/api/v1/chat/confirm",
            json={"conversation_id": cid, "confirmation_id": record.id},
        )
        assert resp2.status_code == 404
    finally:
        store.clear()


# ---- Deny and listing API endpoints ----


async def test_chat_deny_endpoint(client, _sessionmaker):
    from backend.services.confirmation_service import get_confirmation_service

    store = get_confirmation_service()
    store.clear()
    try:
        async with _sessionmaker() as session:
            conversations = ConversationService()
            conversation = await conversations.create(session, agent="beru_core")
            await conversations.add_message(
                session, conversation, role="user", content="run echo deny_test"
            )
            await session.commit()
            cid = conversation.id

        record = store.create(
            conversation_id=cid,
            agent="beru_core",
            tool_name="run_command",
            arguments={"command": "echo deny_test"},
            tool_call_id="call_d1",
        )

        resp = await client.post(
            "/api/v1/chat/deny",
            json={"conversation_id": cid, "confirmation_id": record.id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "denied"
        assert body["tool"] == "run_command"
        assert body["confirmation_id"] == record.id

        # Single-use — denying again returns 404.
        resp2 = await client.post(
            "/api/v1/chat/deny",
            json={"conversation_id": cid, "confirmation_id": record.id},
        )
        assert resp2.status_code == 404

        # Approve also returns 404 — deny consumed the record.
        resp3 = await client.post(
            "/api/v1/chat/confirm",
            json={"conversation_id": cid, "confirmation_id": record.id},
        )
        assert resp3.status_code == 404
    finally:
        store.clear()


async def test_chat_deny_wrong_conversation_returns_404(client, _sessionmaker):
    from backend.services.confirmation_service import get_confirmation_service

    store = get_confirmation_service()
    store.clear()
    try:
        async with _sessionmaker() as session:
            conversations = ConversationService()
            conversation = await conversations.create(session, agent="beru_core")
            await conversations.add_message(
                session, conversation, role="user", content="run echo wrong_conv"
            )
            await session.commit()
            cid = conversation.id

        record = store.create(
            conversation_id=cid,
            agent="beru_core",
            tool_name="run_command",
            arguments={"command": "echo wrong_conv"},
            tool_call_id="call_d2",
        )

        resp = await client.post(
            "/api/v1/chat/deny",
            json={"conversation_id": "conv_other", "confirmation_id": record.id},
        )
        assert resp.status_code == 404
        # Record is NOT consumed by the failed attempt.
        assert store.get(record.id) is not None
    finally:
        store.clear()


async def test_chat_confirmations_list(client, _sessionmaker):
    from backend.services.confirmation_service import get_confirmation_service

    store = get_confirmation_service()
    store.clear()
    try:
        async with _sessionmaker() as session:
            conversations = ConversationService()
            conversation = await conversations.create(session, agent="beru_core")
            await conversations.add_message(
                session, conversation, role="user", content="run multiple tools"
            )
            await session.commit()
            cid = conversation.id

        r1 = store.create(
            conversation_id=cid,
            agent="beru_core",
            tool_name="run_command",
            arguments={"command": "echo one"},
            tool_call_id="call_list1",
        )
        r2 = store.create(
            conversation_id=cid,
            agent="beru_core",
            tool_name="run_command",
            arguments={"command": "echo two"},
            tool_call_id="call_list2",
        )

        resp = await client.get("/api/v1/chat/confirmations", params={"conversation_id": cid})
        assert resp.status_code == 200
        ids = {p["confirmation_id"] for p in resp.json()["pending_confirmations"]}
        assert r1.id in ids and r2.id in ids

        # Deny one and the list shrinks.
        await client.post(
            "/api/v1/chat/deny",
            json={"conversation_id": cid, "confirmation_id": r1.id},
        )
        resp2 = await client.get("/api/v1/chat/confirmations", params={"conversation_id": cid})
        ids2 = {p["confirmation_id"] for p in resp2.json()["pending_confirmations"]}
        assert r1.id not in ids2 and r2.id in ids2
    finally:
        store.clear()