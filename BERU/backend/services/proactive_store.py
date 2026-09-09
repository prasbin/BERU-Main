"""Durable persistence for scheduled tasks and monitor triggers.

The scheduler and monitor engines are intentionally in-memory, so anything
provisioned through the API would be lost on restart. This module is the
write-through / read-back layer between those engines and the database:

* the API routers and the runtime's state hooks persist every mutation here;
* :func:`load_scheduled_tasks` / :func:`load_monitor_triggers` return the same
  ``ScheduledTask`` / ``Trigger`` value objects the engines operate on, so a
  task or trigger behaves identically before and after a restart (schedule,
  status, counters, payload).

SQLite stores naive datetimes even for ``DateTime(timezone=True)`` columns, so
values are normalised to timezone-aware UTC on read (and again on write) to keep
engine timestamp arithmetic correct.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.engines.monitor import (
    ConditionType,
    Trigger,
    TriggerCondition,
    TriggerStatus,
)
from backend.engines.scheduler import ScheduledTask, TaskStatus, TaskType

logger = logging.getLogger(__name__)


def _as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` as a timezone-aware UTC datetime (``None`` passthrough)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc_or_default(value: datetime | None) -> datetime:
    return _as_utc(value) or datetime.now(timezone.utc)


def _parse_dt(value: Any | None) -> datetime | None:
    """Parse a serialized datetime (ISO string or native) back to aware UTC."""
    if value is None or isinstance(value, datetime):
        return _as_utc(value)
    try:
        return _as_utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


def task_to_record(task: ScheduledTask) -> dict:
    """Return the column values that capture a ``ScheduledTask``."""
    return {
        "name": task.name,
        "description": task.description,
        "handler": task.handler,
        "task_type": task.task_type.value,
        "status": task.status.value,
        "interval_seconds": task.interval_seconds,
        "run_at": _as_utc(task.run_at),
        "cron_expr": task.cron_expr,
        "last_run": _as_utc(task.last_run),
        "next_run": _as_utc(task.next_run),
        "run_count": task.run_count,
        "max_runs": task.max_runs,
        "last_error": task.last_error,
        "payload": task.payload or {},
        "agent": task.agent,
        "retention_days": task.retention_days,
        "keep_last": task.keep_last,
    }


def record_to_task(record) -> ScheduledTask:
    """Rebuild a ``ScheduledTask`` from its persisted record."""
    return ScheduledTask(
        id=record.id,
        name=record.name,
        description=record.description,
        handler=record.handler,
        task_type=TaskType(record.task_type),
        status=TaskStatus(record.status),
        interval_seconds=record.interval_seconds,
        run_at=_as_utc(record.run_at),
        cron_expr=record.cron_expr,
        last_run=_as_utc(record.last_run),
        next_run=_as_utc(record.next_run),
        run_count=record.run_count,
        max_runs=record.max_runs,
        last_error=record.last_error,
        payload=record.payload or {},
        agent=record.agent,
        retention_days=record.retention_days,
        keep_last=record.keep_last,
    )


async def save_scheduled_task(session: AsyncSession, task: ScheduledTask) -> None:
    """Insert or update the persisted copy of a scheduled task."""
    from backend.models.scheduled_task import ScheduledTaskRecord

    record = await session.get(ScheduledTaskRecord, task.id)
    created = record is None
    if created:
        record = ScheduledTaskRecord(id=task.id)
        session.add(record)
    for column, value in task_to_record(task).items():
        setattr(record, column, value)
    await session.flush()


async def delete_scheduled_task(session: AsyncSession, task_id: str) -> bool:
    """Remove a persisted scheduled task. Returns False when it had no row."""
    from backend.models.scheduled_task import ScheduledTaskRecord

    record = await session.get(ScheduledTaskRecord, task_id)
    if record is None:
        return False
    await session.delete(record)
    await session.flush()
    return True


async def load_scheduled_tasks(session: AsyncSession) -> list[ScheduledTask]:
    """Load every persisted scheduled task, oldest first."""
    from backend.models.scheduled_task import ScheduledTaskRecord

    rows = (
        await session.execute(
            select(ScheduledTaskRecord).order_by(ScheduledTaskRecord.created_at)
        )
    ).scalars().all()
    return [record_to_task(row) for row in rows]


# ---------------------------------------------------------------------------
# Monitor triggers
# ---------------------------------------------------------------------------


def trigger_to_record(trigger: Trigger) -> dict:
    """Return the column values that capture a ``Trigger``."""
    return {
        "name": trigger.name,
        "description": trigger.description,
        "status": trigger.status.value,
        "condition": {
            "condition_type": trigger.condition.condition_type.value,
            "source": trigger.condition.source,
            "operator": trigger.condition.operator,
            "value": trigger.condition.value,
            "params": trigger.condition.params or {},
        },
        "actions": trigger.actions or [],
        "last_fired": _as_utc(trigger.last_fired),
        "fire_count": trigger.fire_count,
        "cooldown_seconds": trigger.cooldown_seconds,
        "retention_days": trigger.retention_days,
        "keep_last": trigger.keep_last,
    }


def record_to_trigger(record) -> Trigger:
    """Rebuild a ``Trigger`` from its persisted record."""
    condition = record.condition or {}
    return Trigger(
        id=record.id,
        name=record.name,
        description=record.description,
        status=TriggerStatus(record.status),
        condition=TriggerCondition(
            condition_type=ConditionType(condition.get("condition_type", "custom")),
            source=condition.get("source", ""),
            operator=condition.get("operator", "eq"),
            value=condition.get("value"),
            params=condition.get("params") or {},
        ),
        actions=record.actions or [],
        last_fired=_as_utc(record.last_fired),
        fire_count=record.fire_count,
        cooldown_seconds=record.cooldown_seconds,
        retention_days=record.retention_days,
        keep_last=record.keep_last,
        created_at=_as_utc_or_default(record.created_at),
    )


async def save_monitor_trigger(session: AsyncSession, trigger: Trigger) -> None:
    """Insert or update the persisted copy of a monitor trigger."""
    from backend.models.monitor_trigger import MonitorTriggerRecord

    record = await session.get(MonitorTriggerRecord, trigger.id)
    created = record is None
    if created:
        record = MonitorTriggerRecord(id=trigger.id)
        session.add(record)
    for column, value in trigger_to_record(trigger).items():
        setattr(record, column, value)
    await session.flush()


async def delete_monitor_trigger(session: AsyncSession, trigger_id: str) -> bool:
    """Remove a persisted monitor trigger. Returns False when it had no row."""
    from backend.models.monitor_trigger import MonitorTriggerRecord

    record = await session.get(MonitorTriggerRecord, trigger_id)
    if record is None:
        return False
    await session.delete(record)
    await session.flush()
    return True


async def load_monitor_triggers(session: AsyncSession) -> list[Trigger]:
    """Load every persisted monitor trigger, oldest first."""
    from backend.models.monitor_trigger import MonitorTriggerRecord

    rows = (
        await session.execute(
            select(MonitorTriggerRecord).order_by(MonitorTriggerRecord.created_at)
        )
    ).scalars().all()
    return [record_to_trigger(row) for row in rows]


# ---------------------------------------------------------------------------
# Run / fire history (append-only audit logs)
# ---------------------------------------------------------------------------


def task_run_to_dict(record) -> dict:
    """Serialize a ``TaskRunRecord`` for API responses."""
    return {
        "id": record.id,
        "task_id": record.task_id,
        "run_at": _as_utc(record.run_at).isoformat() if record.run_at else None,
        "status": record.status,
        "duration_ms": record.duration_ms,
        "run_count": record.run_count,
        "error": record.error,
        "seq": record.seq,
    }


def trigger_fire_to_dict(record) -> dict:
    """Serialize a ``TriggerFireRecord`` for API responses."""
    return {
        "id": record.id,
        "trigger_id": record.trigger_id,
        "fired_at": _as_utc(record.fired_at).isoformat() if record.fired_at else None,
        "condition": record.condition or {},
        "value": record.value,
        "seq": record.seq,
    }


async def record_task_run(
    session: AsyncSession,
    *,
    task_id: str,
    run_at: datetime | None = None,
    status: str,
    duration_ms: float | None = None,
    run_count: int = 0,
    error: str | None = None,
) -> object:
    """Append one scheduler run to the history log."""
    from backend.models.task_run import TaskRunRecord

    # ``seq`` is assigned atomically inside the INSERT statement (max+1) so
    # ordering by ``seq`` is deterministic even for same-timestamp rows and
    # under concurrent writers.
    stmt = (
        insert(TaskRunRecord)
        .values(
            task_id=task_id,
            run_at=_as_utc_or_default(run_at),
            status=status,
            duration_ms=duration_ms,
            run_count=run_count,
            error=error,
            seq=select(func.coalesce(func.max(TaskRunRecord.seq), 0) + 1).scalar_subquery(),
        )
        .returning(TaskRunRecord)
    )
    return await session.scalar(stmt)


async def list_task_runs(
    session: AsyncSession,
    task_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """Return a task's run history, newest first (datetimes normalised to UTC)."""
    from backend.models.task_run import TaskRunRecord

    rows = (
        await session.execute(
            select(TaskRunRecord)
            .where(TaskRunRecord.task_id == task_id)
            .order_by(
                TaskRunRecord.run_at.desc(),
                TaskRunRecord.seq.desc(),
                TaskRunRecord.created_at.desc(),
                TaskRunRecord.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    for row in rows:
        session.expunge(row)
        row.run_at = _as_utc(row.run_at)
    return list(rows)


async def record_trigger_fire(
    session: AsyncSession,
    *,
    trigger_id: str,
    fired_at: datetime | None = None,
    condition: dict | None = None,
    value: Any = None,
) -> object:
    """Append one trigger firing to the history log."""
    from backend.models.trigger_fire import TriggerFireRecord

    # ``seq`` is assigned atomically inside the INSERT statement (max+1) so
    # ordering by ``seq`` is deterministic even for same-timestamp rows and
    # under concurrent writers.
    stmt = (
        insert(TriggerFireRecord)
        .values(
            trigger_id=trigger_id,
            fired_at=_as_utc_or_default(fired_at),
            condition=condition or {},
            value=value,
            seq=select(func.coalesce(func.max(TriggerFireRecord.seq), 0) + 1).scalar_subquery(),
        )
        .returning(TriggerFireRecord)
    )
    return await session.scalar(stmt)


async def list_trigger_fires(
    session: AsyncSession,
    trigger_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """Return a trigger's fire history, newest first (datetimes normalised)."""
    from backend.models.trigger_fire import TriggerFireRecord

    rows = (
        await session.execute(
            select(TriggerFireRecord)
            .where(TriggerFireRecord.trigger_id == trigger_id)
            .order_by(
                TriggerFireRecord.fired_at.desc(),
                TriggerFireRecord.seq.desc(),
                TriggerFireRecord.created_at.desc(),
                TriggerFireRecord.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    for row in rows:
        session.expunge(row)
        row.fired_at = _as_utc(row.fired_at)
    return list(rows)


# ---------------------------------------------------------------------------
# History counts / summaries / pruning (retention)
# ---------------------------------------------------------------------------


async def count_task_runs(session: AsyncSession, task_id: str) -> int:
    """Return the total number of run-history rows for a task."""
    from backend.models.task_run import TaskRunRecord

    total = await session.scalar(
        select(func.count(TaskRunRecord.id)).where(TaskRunRecord.task_id == task_id)
    )
    return int(total or 0)


async def task_run_summary(session: AsyncSession, task_id: str) -> dict:
    """Aggregate a task's run history: counts, outcomes, and duration stats."""
    from backend.models.task_run import TaskRunRecord

    total = await count_task_runs(session, task_id)
    failed = await session.scalar(
        select(func.count(TaskRunRecord.id)).where(
            TaskRunRecord.task_id == task_id,
            TaskRunRecord.status == "failed",
        )
    )
    failed = int(failed or 0)

    row = (
        await session.execute(
            select(
                func.avg(TaskRunRecord.duration_ms),
                func.min(TaskRunRecord.duration_ms),
                func.max(TaskRunRecord.duration_ms),
                func.min(TaskRunRecord.run_at),
                func.max(TaskRunRecord.run_at),
            ).where(TaskRunRecord.task_id == task_id)
        )
    ).one()
    avg_duration_ms, min_duration_ms, max_duration_ms, first_run, last_run = row

    error_groups_rows = (
        await session.execute(
            select(
                TaskRunRecord.error,
                func.count(TaskRunRecord.id),
                func.max(TaskRunRecord.run_at),
            )
            .where(
                TaskRunRecord.task_id == task_id,
                TaskRunRecord.error.is_not(None),
            )
            .group_by(TaskRunRecord.error)
            .order_by(func.count(TaskRunRecord.id).desc())
        )
    ).all()
    error_groups = [
        {
            "error": error,
            "count": int(count),
            "last_run": _as_utc(last_run).isoformat() if last_run else None,
        }
        for error, count, last_run in error_groups_rows
    ]

    return {
        "total": total,
        "failed": failed,
        "success_rate": (
            round((total - failed) / total, 3) if total else None
        ),
        "failure_rate": (round(failed / total, 3) if total else None),
        "last_error": await task_last_error(session, task_id),
        "avg_duration_ms": (
            round(float(avg_duration_ms), 2) if avg_duration_ms is not None else None
        ),
        "min_duration_ms": float(min_duration_ms) if min_duration_ms is not None else None,
        "max_duration_ms": float(max_duration_ms) if max_duration_ms is not None else None,
        "first_run": _as_utc(first_run).isoformat() if first_run else None,
        "last_run": _as_utc(last_run).isoformat() if last_run else None,
        "error_groups": error_groups,
    }


async def task_failure_rate(session: AsyncSession, task_id: str) -> float:
    """Return a task's failure rate (0.0-1.0); 0.0 when there are no runs."""
    from backend.models.task_run import TaskRunRecord

    total = await count_task_runs(session, task_id)
    if total == 0:
        return 0.0
    failed = await session.scalar(
        select(func.count(TaskRunRecord.id)).where(
            TaskRunRecord.task_id == task_id,
            TaskRunRecord.status == "failed",
        )
    )
    return round((int(failed or 0) / total), 3)


async def task_last_error(session: AsyncSession, task_id: str) -> str | None:
    """Return the most recent failed run's error message for a task."""
    from backend.models.task_run import TaskRunRecord

    row = (
        await session.execute(
            select(TaskRunRecord.error)
            .where(
                TaskRunRecord.task_id == task_id,
                TaskRunRecord.status == "failed",
                TaskRunRecord.error.is_not(None),
            )
            .order_by(
                TaskRunRecord.run_at.desc(),
                TaskRunRecord.seq.desc(),
                TaskRunRecord.created_at.desc(),
                TaskRunRecord.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def count_task_error_groups(session: AsyncSession, task_id: str) -> int:
    """Return the number of distinct error messages in a task's run history."""
    from backend.models.task_run import TaskRunRecord

    total = await session.scalar(
        select(func.count(func.distinct(TaskRunRecord.error))).where(
            TaskRunRecord.task_id == task_id,
            TaskRunRecord.error.is_not(None),
        )
    )
    return int(total or 0)


async def count_trigger_fires(session: AsyncSession, trigger_id: str) -> int:
    """Return the total number of fire-history rows for a trigger."""
    from backend.models.trigger_fire import TriggerFireRecord

    total = await session.scalar(
        select(func.count(TriggerFireRecord.id)).where(
            TriggerFireRecord.trigger_id == trigger_id
        )
    )
    return int(total or 0)


async def trigger_last_value(session: AsyncSession, trigger_id: str) -> Any:
    """Return the most recent satisfying value recorded for a trigger.

    Fires are ordered newest-first by ``fired_at``; rows that share the same
    ``fired_at`` timestamp (possible at microsecond precision) are ordered by
    the monotonic ``seq`` insert counter, so the most recently written row is
    always returned — never the random row ``id``.
    """
    from backend.models.trigger_fire import TriggerFireRecord

    row = (
        await session.execute(
            select(TriggerFireRecord.value)
            .where(TriggerFireRecord.trigger_id == trigger_id)
            .order_by(
                TriggerFireRecord.fired_at.desc(),
                TriggerFireRecord.seq.desc(),
                TriggerFireRecord.created_at.desc(),
                TriggerFireRecord.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def trigger_fire_summary(session: AsyncSession, trigger_id: str) -> dict:
    """Aggregate a trigger's fire history: count + first/last fires."""
    from backend.models.trigger_fire import TriggerFireRecord

    total = await count_trigger_fires(session, trigger_id)
    row = (
        await session.execute(
            select(
                func.min(TriggerFireRecord.fired_at),
                func.max(TriggerFireRecord.fired_at),
            ).where(TriggerFireRecord.trigger_id == trigger_id)
        )
    ).one()
    first_fired, last_fired = row

    return {
        "total": total,
        "first_fired": _as_utc(first_fired).isoformat() if first_fired else None,
        "last_fired": _as_utc(last_fired).isoformat() if last_fired else None,
    }


async def prune_task_runs(
    session: AsyncSession,
    task_id: str,
    *,
    retention_seconds: float | None = None,
    keep_last: int | None = None,
) -> int:
    """Delete run-history rows older than `retention_seconds` and/or beyond the
    newest `keep_last` rows. Returns the number of rows removed."""
    from backend.models.task_run import TaskRunRecord

    deleted = 0
    if retention_seconds is not None and retention_seconds > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention_seconds)
        result = await session.execute(
            delete(TaskRunRecord).where(
                TaskRunRecord.task_id == task_id,
                TaskRunRecord.run_at < cutoff,
            )
        )
        deleted += int(result.rowcount or 0)
    if keep_last is not None and keep_last >= 0:
        newest = (
            select(TaskRunRecord.id)
            .where(TaskRunRecord.task_id == task_id)
            .order_by(TaskRunRecord.run_at.desc(), TaskRunRecord.seq.desc())
            .limit(keep_last)
            .subquery()
        )
        result = await session.execute(
            delete(TaskRunRecord).where(
                TaskRunRecord.task_id == task_id,
                TaskRunRecord.id.not_in(select(newest.c.id)),
            )
        )
        deleted += int(result.rowcount or 0)
    await session.flush()
    return deleted


async def prune_trigger_fires(
    session: AsyncSession,
    trigger_id: str,
    *,
    retention_seconds: float | None = None,
    keep_last: int | None = None,
) -> int:
    """Delete fire-history rows older than `retention_seconds` and/or beyond the
    newest `keep_last` rows. Returns the number of rows removed."""
    from backend.models.trigger_fire import TriggerFireRecord

    deleted = 0
    if retention_seconds is not None and retention_seconds > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention_seconds)
        result = await session.execute(
            delete(TriggerFireRecord).where(
                TriggerFireRecord.trigger_id == trigger_id,
                TriggerFireRecord.fired_at < cutoff,
            )
        )
        deleted += int(result.rowcount or 0)
    if keep_last is not None and keep_last >= 0:
        newest = (
            select(TriggerFireRecord.id)
            .where(TriggerFireRecord.trigger_id == trigger_id)
            .order_by(TriggerFireRecord.fired_at.desc(), TriggerFireRecord.seq.desc())
            .limit(keep_last)
            .subquery()
        )
        result = await session.execute(
            delete(TriggerFireRecord).where(
                TriggerFireRecord.trigger_id == trigger_id,
                TriggerFireRecord.id.not_in(select(newest.c.id)),
            )
        )
        deleted += int(result.rowcount or 0)
    await session.flush()
    return deleted


# ---------------------------------------------------------------------------
# History export / restore (backup of the append-only logs)
# ---------------------------------------------------------------------------


async def export_task_runs(session: AsyncSession) -> list[dict]:
    """Serialize every run-history row (oldest first) as a JSON-safe list.

    Rows keep their stable ``id`` so an export can be restored idempotently.
    """
    from backend.models.task_run import TaskRunRecord

    rows = (
        await session.execute(
            select(TaskRunRecord).order_by(
                TaskRunRecord.run_at, TaskRunRecord.seq
            )
        )
    ).scalars().all()
    return [task_run_to_dict(r) for r in rows]


async def export_trigger_fires(session: AsyncSession) -> list[dict]:
    """Serialize every fire-history row (oldest first) as a JSON-safe list."""
    from backend.models.trigger_fire import TriggerFireRecord

    rows = (
        await session.execute(
            select(TriggerFireRecord).order_by(
                TriggerFireRecord.fired_at, TriggerFireRecord.seq
            )
        )
    ).scalars().all()
    return [trigger_fire_to_dict(r) for r in rows]


async def import_task_runs(
    session: AsyncSession, rows: list[dict], *, replace: bool = False
) -> dict:
    """Restore run-history rows from an export document.

    Rows whose ``id`` already exists are skipped unless ``replace`` is set, so
    an export can be applied more than once without duplicating history.
    Returns ``{"inserted", "skipped", "replaced"}`` counts.
    """
    from backend.models.task_run import TaskRunRecord

    inserted = skipped = replaced = 0
    next_seq = int(
        await session.scalar(select(func.coalesce(func.max(TaskRunRecord.seq), 0))) or 0
    )
    for data in rows or []:
        record_id = data.get("id")
        record = await session.get(TaskRunRecord, record_id) if record_id else None
        if record is not None:
            if not replace:
                skipped += 1
                continue
            replaced += 1
        else:
            record = TaskRunRecord(id=record_id) if record_id else TaskRunRecord()
            session.add(record)
            inserted += 1
        record.task_id = data.get("task_id", "")
        record.run_at = _as_utc_or_default(_parse_dt(data.get("run_at")))
        record.status = data.get("status", "completed")
        record.duration_ms = data.get("duration_ms")
        record.run_count = data.get("run_count", 0)
        record.error = data.get("error")
        record.seq = data.get("seq") or (next_seq := next_seq + 1)
    await session.flush()
    return {"inserted": inserted, "skipped": skipped, "replaced": replaced}


async def import_trigger_fires(
    session: AsyncSession, rows: list[dict], *, replace: bool = False
) -> dict:
    """Restore fire-history rows from an export document (idempotent by ``id``)."""
    from backend.models.trigger_fire import TriggerFireRecord

    inserted = skipped = replaced = 0
    next_seq = int(
        await session.scalar(select(func.coalesce(func.max(TriggerFireRecord.seq), 0))) or 0
    )
    for data in rows or []:
        record_id = data.get("id")
        record = await session.get(TriggerFireRecord, record_id) if record_id else None
        if record is not None:
            if not replace:
                skipped += 1
                continue
            replaced += 1
        else:
            record = TriggerFireRecord(id=record_id) if record_id else TriggerFireRecord()
            session.add(record)
            inserted += 1
        record.trigger_id = data.get("trigger_id", "")
        record.fired_at = _as_utc_or_default(_parse_dt(data.get("fired_at")))
        record.condition = data.get("condition") or {}
        record.value = data.get("value")
        record.seq = data.get("seq") or (next_seq := next_seq + 1)
    await session.flush()
    return {"inserted": inserted, "skipped": skipped, "replaced": replaced}