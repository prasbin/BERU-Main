"""Tests for the durable reliability ledger (DB persistence + restore + prune).

The in-memory ledger becomes durable when a session factory is installed: every
recorded entry is written best-effort to ``activity_records`` /
``audit_records``, and :func:`restore_activity_ledger` reloads the newest rows
into memory (pruning anything beyond the bounded window) so observability
survives restarts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from backend.agents.base import BaseAgent, ToolCall
from backend.models.activity_record import ActivityRecord
from backend.models.audit_record import AuditRecord
from backend.services.activity_ledger import (
    ActivityEntry,
    AuditEntry,
    get_activity_ledger,
    reset_activity_ledger,
    restore_activity_ledger,
    set_ledger_session_factory,
)


@pytest.fixture
def _disable_persistence():
    reset_activity_ledger()
    yield
    reset_activity_ledger()


async def test_not_persisted_when_no_factory(_sessionmaker, _disable_persistence):
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1.0, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    await ledger.flush()
    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(ActivityRecord.seq)))
    assert total == 0


async def test_activity_persisted_and_restored(_sessionmaker, _disable_persistence):
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    for i in range(3):
        ledger.record_activity(ActivityEntry(
            timestamp=float(i + 1), request_id=f"r{i}", agent="beru_core",
            tool_name="clock", args_summary="{}", outcome="success",
            duration_ms=1.0, error=None,
        ))
    await ledger.flush()

    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(ActivityRecord.seq)))
    assert total == 3

    # Simulate a restart: fresh singleton, then restore from the DB.
    reset_activity_ledger()
    restored = await restore_activity_ledger(session_factory=lambda: _sessionmaker)
    assert restored == {"activity": 3, "audit": 0}

    entries = get_activity_ledger().list_activity(limit=100)
    assert len(entries) == 3
    assert entries[0]["request_id"] == "r0"
    assert entries[-1]["request_id"] == "r2"
    assert entries[-1]["outcome"] == "success"


async def test_audit_persisted_and_restored(_sessionmaker, _disable_persistence):
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    ledger.record_audit(AuditEntry(
        timestamp=10.0, request_id="r1", agent="beru_core", tool_name="run_command",
        decision="approved", confirmation_id="c1", outcome="success", error=None,
    ))
    ledger.record_audit(AuditEntry(
        timestamp=11.0, request_id="r2", agent="beru_core", tool_name="launch_app",
        decision="denied", confirmation_id="c2", outcome=None, error=None,
    ))
    await ledger.flush()

    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(AuditRecord.seq)))
    assert total == 2

    reset_activity_ledger()
    restored = await restore_activity_ledger(session_factory=lambda: _sessionmaker)
    assert restored == {"activity": 0, "audit": 2}
    entries = get_activity_ledger().list_audit(limit=100)
    assert [e["decision"] for e in entries] == ["approved", "denied"]


async def test_run_tool_persists_activity_row(_sessionmaker, _disable_persistence):
    from backend.tools.clock import ClockTool

    set_ledger_session_factory(lambda: _sessionmaker)
    agent = BaseAgent()
    agent.register_tool(ClockTool())
    result = await agent.run_tool(
        ToolCall(id="t1", name="clock", arguments="{}"), agent_name="beru_core"
    )
    assert result.ok is True
    await get_activity_ledger().flush()

    async with _sessionmaker() as session:
        rows = (await session.scalars(select(ActivityRecord))).all()
    assert len(rows) == 1
    assert rows[0].tool_name == "clock"
    assert rows[0].outcome == "success"
    assert rows[0].agent == "beru_core"


async def test_restore_prunes_db_to_bounds(_sessionmaker, _disable_persistence):
    # Insert 600 rows directly (past the in-memory bound) so the prune is real.
    async with _sessionmaker() as session:
        for i in range(600):
            session.add(ActivityRecord(
                seq=i, timestamp=float(i), request_id=f"r{i}", agent="beru_core",
                tool_name="clock", args_summary="{}", outcome="success",
                duration_ms=1.0, error=None,
            ))
        await session.commit()

    restored = await restore_activity_ledger(session_factory=lambda: _sessionmaker)
    assert restored["activity"] == 500

    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(ActivityRecord.seq)))
    assert total == 500
    assert len(get_activity_ledger().list_activity(limit=10**6)) == 500


async def test_clear_purges_db(_sessionmaker, _disable_persistence):
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1.0, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
))
    await ledger.flush()
    ledger.clear()
    await ledger.purge()

    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(ActivityRecord.seq)))
    assert total == 0
    assert ledger.list_activity() == []


async def test_delete_endpoint_purges_db(client, _sessionmaker, _disable_persistence):
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1.0, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    await ledger.flush()

    async with _sessionmaker() as session:
        before = await session.scalar(select(func.count(ActivityRecord.seq)))

    resp = await client.delete("/api/v1/reliability/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cleared"] is True
    assert before == 1
    assert data["purged"] == 1


async def test_purge_flushes_pending_writes_before_deleting(_sessionmaker, _disable_persistence):
    """Rows recorded but not yet flushed are settled before the purge DELETE runs.

    Without the flush-in-purge, the fire-and-forget persistence tasks would
    resurrect rows immediately after the DELETE (a 1-2 line race that makes
    "clear ledger" unreliable under load).
    """
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1.0, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    # NOTE: no awaiting of flush()/purge() persistence tasks here by design.
    ledger.clear()
    removed = await ledger.purge()

    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(ActivityRecord.seq)))
    assert removed >= 1
    assert total == 0


async def test_restore_is_idempotent(_sessionmaker, _disable_persistence):
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=1.0, request_id="r", agent="beru_core", tool_name="clock",
        args_summary="{}", outcome="success", duration_ms=1.0, error=None,
    ))
    await ledger.flush()

    reset_activity_ledger()
    await restore_activity_ledger(session_factory=lambda: _sessionmaker)
    await restore_activity_ledger(session_factory=lambda: _sessionmaker)
    assert len(get_activity_ledger().list_activity(limit=10**6)) == 1


async def test_restart_ordering_uses_timestamp_not_seq(_sessionmaker, _disable_persistence):
    """New records stay newest after a restart even though ``seq`` resets.

    ``seq`` is a Python-side counter that starts from zero in every process, so
    a new process's rows can carry LOWER seq numbers than old rows. Restore must
    order purely by wall-clock ``timestamp`` (with ``seq`` only breaking exact
    ties), prune the genuinely old rows, and keep the new ones newest.
    """
    import backend.services.activity_ledger as ledger_module

    # Process 1: three old rows with early wall-clock timestamps.
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    for i in range(3):
        ledger.record_activity(ActivityEntry(
            timestamp=float(i + 1), request_id=f"old{i}", agent="beru_core",
            tool_name="clock", args_summary="{}", outcome="success",
            duration_ms=1.0, error=None,
        ))
    await ledger.flush()

    # Simulate a genuine restart: fresh singleton AND a zeroed seq counter.
    reset_activity_ledger()
    ledger_module._seq_counter = 0  # noqa: SLF001 - simulate new process
    set_ledger_session_factory(lambda: _sessionmaker)

    # Process 2: new rows with later timestamps (their seq numbers restart at 1).
    ledger = get_activity_ledger()
    for ts, rid in ((100.0, "new0"), (200.0, "new1"), (300.0, "new2")):
        ledger.record_activity(ActivityEntry(
            timestamp=ts, request_id=rid, agent="beru_core", tool_name="clock",
            args_summary="{}", outcome="success", duration_ms=1.0, error=None,
        ))
    await ledger.flush()

    # Restarted again: a bounded restore keeps only the two newest rows.
    reset_activity_ledger()
    restored = await restore_activity_ledger(
        session_factory=lambda: _sessionmaker, activity_limit=2
    )
    assert restored == {"activity": 2, "audit": 0}

    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(ActivityRecord.seq)))
    assert total == 2  # the three old rows were pruned, new ones kept

    entries = get_activity_ledger().list_activity(limit=10)
    assert [e["request_id"] for e in entries] == ["new1", "new2"]
    assert [e["timestamp"] for e in entries] == [200.0, 300.0]


async def test_restart_rebases_seq_for_timestamp_tiebreaks(
    _sessionmaker, _disable_persistence
):
    """A new row sharing a wall-clock timestamp with a pre-restart row stays newest.

    The process-local seq counter resets on boot; restore re-bases it to the
    persisted high-water mark, so a post-restart row keeps strictly higher seq
    than any pre-restart row. Without the re-base, the tie-break would collapse
    (duplicate seq values) and the OLD row could be recalled as the newest —
    and be the one kept when the window prunes down to the timestamp
    cutoff.
    """
    import backend.services.activity_ledger as ledger_module

    # Process 1: two rows, the second at ts=100.0.
    set_ledger_session_factory(lambda: _sessionmaker)
    ledger = get_activity_ledger()
    for ts, rid in ((50.0, "old0"), (100.0, "old1")):
        ledger.record_activity(ActivityEntry(
            timestamp=ts,
            request_id=rid,
            agent="beru_core",
            tool_name="clock",
            args_summary="{}",
            outcome="success",
            duration_ms=1.0,
            error=None,
        ))
    await ledger.flush()

    # Genuine restart: fresh singleton, zeroed counter, and a restore pass
    # (which re-bases the seq counter on the persisted high-water mark).
    reset_activity_ledger()
    ledger_module._seq_counter = 0  # noqa: SLF001 - simulate new process
    set_ledger_session_factory(lambda: _sessionmaker)
    await restore_activity_ledger(session_factory=lambda: _sessionmaker)

    # Process 2: a new row at the exact same wall-clock tick as "old1".
    ledger = get_activity_ledger()
    ledger.record_activity(ActivityEntry(
        timestamp=100.0,
        request_id="new0",
        agent="beru_core",
        tool_name="clock",
        args_summary="{}",
        outcome="success",
        duration_ms=1.0,
        error=None,
    ))
    await ledger.flush()

    async with _sessionmaker() as session:
        rows = (await session.scalars(select(ActivityRecord))).all()
    seqs = {r.request_id: r.seq for r in rows}
    assert seqs["new0"] == seqs["old1"] + 1  # seq continues across the restart

    # Bounded restore: new0 is recalled as newest; the timestamp cutoff cannot
    # prune it (only rows strictly older than the cutoff are deleted).
    reset_activity_ledger()
    restored = await restore_activity_ledger(
        session_factory=lambda: _sessionmaker, activity_limit=2
    )
    assert restored["activity"] == 2
    entries = get_activity_ledger().list_activity(limit=10)
    assert [e["request_id"] for e in entries] == ["old1", "new0"]
    assert entries[-1]["request_id"] == "new0"  # the NEW row holds the newest slot
    assert [e["timestamp"] for e in entries] == [100.0, 100.0]

    async with _sessionmaker() as session:
        total = await session.scalar(select(func.count(ActivityRecord.seq)))
    assert total == 2  # old0 (ts=50) pruned, the tied new row kept
