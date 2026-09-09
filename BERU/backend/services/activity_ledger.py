"""In-memory activity ledger and approval audit trail (with optional durability).

Keeps bounded ring buffers of recent tool-call activity and approval decisions.
Thread-safe via a module-level lock.

Durability: when a session factory is installed via
:func:`set_ledger_session_factory`, every recorded entry is also written
best-effort to the ``activity_records`` / ``audit_records`` tables (fire-and
forget, so recording never blocks tool execution). :func:`restore_activity_ledger`
reloads the newest entries into the buffers at startup and prunes rows beyond
the bounded window. Without a factory the ledger is purely in-memory (safe,
hermetic default).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

_ACTIVITY_MAX = 500
_AUDIT_MAX = 200
_ARGS_SUMMARY_MAX = 200
_ERROR_SUMMARY_MAX = 200

logger = logging.getLogger(__name__)

#: Cap on in-flight background persist tasks. Beyond this, new writes stay
#: memory-only (with a warning) so a crash-looping DB or a write storm cannot
#: accumulate unbounded pending tasks.
_MAX_PENDING_PERSISTS = 256

#: Counter stamping persisted rows with a tie-breaker for the rare case where
#: two entries share the exact same wall-clock timestamp. Wall-clock
#: ``timestamp`` remains the primary ordering key; this counter only resolves
#: exact ties. :func:`restore_activity_ledger` re-bases it to the highest seq
#: already in the tables, so seq values continue rising across process
#: restarts and never collide with seq numbers left behind by a previous run.
_seq_counter = 0


def _next_seq() -> int:
    global _seq_counter  # noqa: PLW0603
    _seq_counter += 1
    return _seq_counter


def _rebase_seq_counter(floor: int) -> None:
    """Raise the seq counter above a persisted high-water mark.

    Called during restore. Without this, the process-local counter starts at
    zero on every boot and would hand out seq numbers still present in the
    tables from a previous run — turning ``seq`` into an ambiguous tie-breaker
    the moment two rows share a wall-clock timestamp across a restart.
    """
    global _seq_counter  # noqa: PLW0603
    if floor > _seq_counter:
        _seq_counter = floor


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    timestamp: float
    request_id: str
    agent: str
    tool_name: str
    args_summary: str
    outcome: str  # success | failure | confirmation_required | permission_denied | error
    duration_ms: float | None
    error: str | None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    timestamp: float
    request_id: str
    agent: str
    tool_name: str
    decision: str  # approved | denied
    confirmation_id: str
    outcome: str | None  # success | failure | None (for deny)
    error: str | None


class ActivityLedger:
    """Bounded ring buffer storing recent activity and audit entries.

    When a session factory is configured, entries are also persisted to the
    database asynchronously (best-effort). In-flight persistence can be awaited
    with :meth:`flush`, which tests use to make assertions deterministic.
    """

    def __init__(
        self, activity_max: int = _ACTIVITY_MAX, audit_max: int = _AUDIT_MAX,
    ) -> None:
        self._activity: list[dict[str, Any]] = []
        self._audit: list[dict[str, Any]] = []
        self._activity_max = activity_max
        self._audit_max = audit_max
        self._lock = threading.Lock()
        self._pending: set[asyncio.Task] = set()
        self._started_at: float = time.time()

    # ---- Recording ----

    def record_activity(self, entry: ActivityEntry) -> None:
        with self._lock:
            self._activity.append(asdict(entry))
            if len(self._activity) > self._activity_max:
                self._activity = self._activity[-self._activity_max:]
        self._schedule_persist(entry, is_audit=False)

    def record_audit(self, entry: AuditEntry) -> None:
        with self._lock:
            self._audit.append(asdict(entry))
            if len(self._audit) > self._audit_max:
                self._audit = self._audit[-self._audit_max:]
        self._schedule_persist(entry, is_audit=True)

    # ---- Querying ----

    def list_activity(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._activity[-limit:])

    def list_audit(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._audit[-limit:])

    def summary(self) -> dict[str, Any]:
        """Aggregated health/activity summary for ``/status``."""
        with self._lock:
            total = len(self._activity)
            successes = sum(1 for e in self._activity if e.get("outcome") == "success")
            failures = sum(
                1 for e in self._activity
                if e.get("outcome") in ("failure", "error")
            )
            confirmations = sum(
                1 for e in self._activity
                if e.get("outcome") == "confirmation_required"
            )
            approvals = sum(1 for e in self._audit if e.get("decision") == "approved")
            denials = sum(1 for e in self._audit if e.get("decision") == "denied")
            tool_counts: dict[str, int] = {}
            for e in self._activity:
                tn = e.get("tool_name", "")
                tool_counts[tn] = tool_counts.get(tn, 0) + 1
            top_tools = sorted(tool_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
            return {
                "uptime_s": round(time.time() - self._started_at, 1),
                "total_tool_calls": total,
                "successes": successes,
                "failures": failures,
                "confirmations_pending": confirmations,
                "approvals": approvals,
                "denials": denials,
                "top_tools": top_tools,
                "persisted": ledger_persistence_enabled(),
            }

    def replace_all(self, activity: list[dict[str, Any]], audit: list[dict[str, Any]]) -> None:
        """Replace the in-memory buffers (used by restore; already bounded)."""
        with self._lock:
            self._activity = list(activity[-self._activity_max:])
            self._audit = list(audit[-self._audit_max:])

    def clear(self) -> None:
        with self._lock:
            self._activity.clear()
            self._audit.clear()

    # ---- Durability ----

    def _schedule_persist(self, entry: ActivityEntry | AuditEntry, *, is_audit: bool) -> None:
        if _session_factory is None:
            return
        if len(self._pending) >= _MAX_PENDING_PERSISTS:
            logger.warning(
                "Reliability ledger persistence saturated (%d pending); "
                "this entry stays memory-only",
                len(self._pending),
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (e.g. sync callers) — in-memory only
        payload = asdict(entry)
        factory = _session_factory
        task = loop.create_task(self._persist_entry(payload, factory, is_audit=is_audit))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _persist_entry(
        self, payload: dict[str, Any], factory: Callable, *, is_audit: bool,
    ) -> None:
        """Write one entry to the DB. Never raises (best-effort durability)."""
        seq = _next_seq()
        try:
            maker = factory()
            async with maker() as session:
                if is_audit:
                    from backend.models.audit_record import AuditRecord

                    session.add(AuditRecord(
                        seq=seq,
                        timestamp=payload["timestamp"],
                        request_id=payload["request_id"],
                        agent=payload["agent"],
                        tool_name=payload["tool_name"],
                        decision=payload["decision"],
                        confirmation_id=payload["confirmation_id"],
                        outcome=payload["outcome"],
                        error=payload["error"],
                    ))
                else:
                    from backend.models.activity_record import ActivityRecord

                    session.add(ActivityRecord(
                        seq=seq,
                        timestamp=payload["timestamp"],
                        request_id=payload["request_id"],
                        agent=payload["agent"],
                        tool_name=payload["tool_name"],
                        args_summary=payload["args_summary"],
                        outcome=payload["outcome"],
                        duration_ms=payload["duration_ms"],
                        error=payload["error"],
                    ))
                await session.commit()
        except Exception:  # noqa: BLE001 - observability writes must never crash callers
            logger.exception("Failed to persist reliability ledger entry")

    async def flush(self) -> int:
        """Await all in-flight persistence tasks; return the number awaited."""
        tasks = [t for t in list(self._pending) if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def purge(self) -> int:
        """Delete every persisted row (DB half of ``clear``)."""
        # Settle any in-flight persistence first, otherwise those fire-and-forget
        # writes would resurrect rows right after the purge.
        await self.flush()
        if _session_factory is None:
            return 0
        from sqlalchemy import delete

        from backend.models.activity_record import ActivityRecord
        from backend.models.audit_record import AuditRecord

        removed = 0
        try:
            maker = _session_factory()
            async with maker() as session:
                removed += (await session.execute(delete(ActivityRecord))).rowcount or 0
                removed += (await session.execute(delete(AuditRecord))).rowcount or 0
                await session.commit()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.exception("Failed to purge reliability ledger rows")
        return removed

    def _cancel_pending(self) -> None:
        for task in list(self._pending):
            if not task.done():
                try:
                    task.cancel()
                except RuntimeError:  # pragma: no cover - loop already closed
                    pass
        self._pending.clear()


# ---- Helpers for truncating args / errors ----

def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def args_summary(arguments: dict[str, Any]) -> str:
    raw = str(arguments)
    return truncate(raw, _ARGS_SUMMARY_MAX)


def error_summary(error: str | None) -> str | None:
    if error is None:
        return None
    return truncate(error, _ERROR_SUMMARY_MAX)


# ---- DB row conversion (restore) ----


def _row_to_activity(row) -> dict[str, Any]:
    return {
        "timestamp": row.timestamp,
        "request_id": row.request_id,
        "agent": row.agent,
        "tool_name": row.tool_name,
        "args_summary": row.args_summary,
        "outcome": row.outcome,
        "duration_ms": row.duration_ms,
        "error": row.error,
    }


def _row_to_audit(row) -> dict[str, Any]:
    return {
        "timestamp": row.timestamp,
        "request_id": row.request_id,
        "agent": row.agent,
        "tool_name": row.tool_name,
        "decision": row.decision,
        "confirmation_id": row.confirmation_id,
        "outcome": row.outcome,
        "error": row.error,
    }


# ---- Module singleton + durability wiring ----

_ledger: ActivityLedger | None = None
_ledger_factory = ActivityLedger

#: Optional callable returning an async sessionmaker; when set, entries are
#: persisted to the database. ``None`` keeps the ledger purely in-memory.
_session_factory: Callable | None = None


def ledger_persistence_enabled() -> bool:
    return _session_factory is not None


def get_activity_ledger() -> ActivityLedger:
    global _ledger  # noqa: PLW0603
    if _ledger is None:
        _ledger = _ledger_factory()
    return _ledger


def set_ledger_session_factory(factory: Callable | None) -> None:
    """Install (or clear) the DB factory used for best-effort persistence.

    ``factory`` is a callable returning an :class:`async_sessionmaker` (mirrors
    ``proactive_service.set_session_factory``). ``None`` disables persistence.
    """
    global _session_factory  # noqa: PLW0603
    _session_factory = factory


def reset_activity_ledger() -> None:
    """Replace the singleton with a fresh ledger and disable persistence.

    Used by tests/teardown so recorded activity and DB writes never leak across
    cases. Cancels any in-flight persistence tasks.
    """
    global _ledger  # noqa: PLW0603
    if _ledger is not None:
        _ledger._cancel_pending()
    _ledger = None
    set_ledger_session_factory(None)


def set_activity_ledger_factory(factory) -> None:
    """Override the ledger constructor (useful for test isolation)."""
    global _ledger_factory  # noqa: PLW0603
    _ledger_factory = factory
    reset_activity_ledger()


async def restore_activity_ledger(
    session_factory: Callable | None = None,
    activity_limit: int | None = None,
    audit_limit: int | None = None,
) -> dict[str, int]:
    """Reload the newest persisted entries into the in-memory ledger.

    Reads the last ``activity_limit`` (default ``_ACTIVITY_MAX``) activity rows
    and ``audit_limit`` (default ``_AUDIT_MAX``) audit rows into the buffers,
    then prunes any rows older than that window so the tables stay bounded.
    Ordering is by wall-clock ``timestamp``; ``seq`` is used only to break
    exact timestamp ties, and is re-based to the persisted high-water mark so
    it stays monotonic across process restarts (new rows always win a tie
    against rows written by a previous run).
    Uses ``session_factory`` when given, else the installed factory. Returns
    the number of rows restored (``{"activity": n, "audit": n}``).
    """
    from sqlalchemy import delete, func, select

    from backend.models.activity_record import ActivityRecord
    from backend.models.audit_record import AuditRecord

    factory = session_factory if session_factory is not None else _session_factory
    if factory is None:
        return {"activity": 0, "audit": 0}

    act_limit = activity_limit or _ACTIVITY_MAX
    aud_limit = audit_limit or _AUDIT_MAX
    act_rows: list = []
    aud_rows: list = []
    try:
        maker = factory()
        async with maker() as session:
            # Re-base the seq counter on the persisted high-water mark before
            # anything else, so any entry recorded after this restore (or by a
            # surviving task) continues numbering strictly above prior rows.
            persisted_act_max = await session.scalar(
                select(func.max(ActivityRecord.seq))
            )
            persisted_aud_max = await session.scalar(
                select(func.max(AuditRecord.seq))
            )
            high_water = max(
                (v for v in (persisted_act_max, persisted_aud_max) if v is not None),
                default=0,
            )
            if high_water:
                _rebase_seq_counter(high_water)
            act_rows = list((await session.scalars(
                select(ActivityRecord)
                .order_by(ActivityRecord.timestamp.desc(), ActivityRecord.seq.desc())
                .limit(act_limit)
            )).all())
            aud_rows = list((await session.scalars(
                select(AuditRecord)
                .order_by(AuditRecord.timestamp.desc(), AuditRecord.seq.desc())
                .limit(aud_limit)
            )).all())
            if act_rows:
                cutoff = min(r.timestamp for r in act_rows)
                pruned_act = (await session.execute(
                    delete(ActivityRecord).where(ActivityRecord.timestamp < cutoff)
                )).rowcount or 0
            else:
                pruned_act = 0
            if aud_rows:
                cutoff = min(r.timestamp for r in aud_rows)
                pruned_aud = (await session.execute(
                    delete(AuditRecord).where(AuditRecord.timestamp < cutoff)
                )).rowcount or 0
            else:
                pruned_aud = 0
            act_rows.reverse()
            aud_rows.reverse()
            await session.commit()
            if pruned_act or pruned_aud:
                logger.info(
                    "Ledger restore pruned %d activity, %d audit rows beyond window",
                    pruned_act,
                    pruned_aud,
                )
    except Exception:  # noqa: BLE001 - startup must not die on a bad store row
        logger.exception("Failed to restore reliability ledger from storage")
        return {"activity": 0, "audit": 0}

    get_activity_ledger().replace_all(
        activity=[_row_to_activity(r) for r in act_rows],
        audit=[_row_to_audit(r) for r in aud_rows],
    )
    return {"activity": len(act_rows), "audit": len(aud_rows)}