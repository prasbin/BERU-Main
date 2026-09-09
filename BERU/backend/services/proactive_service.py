"""Proactive/autonomous runtime — shared scheduler, monitor, and notifications.

Owns the process-wide singletons used by the ``/scheduler`` and ``/monitor``
routers and started/stopped with the application lifespan:

* :func:`get_scheduler` — background task scheduler with registered handlers
  that run real agent turns (``agent_turn``) or send notifications (``notify``).
* :func:`get_event_monitor` — trigger engine fed by real data sources (host
  CPU/memory, unread-notification volume) whose firing persists and broadcasts
  an alert and executes the trigger's configured actions.
* :func:`get_notification_service` — database-backed, WebSocket-broadcasting
  notification inbox.

Security contract: scheduled agent turns go through the same ChatService as
interactive chat, so confirmation-required tools are **never auto-executed** —
they surface as pending confirmations (and an alert) for the user to approve.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from backend.engines.monitor import EventMonitor, Trigger
from backend.engines.notifications import (
    Notification,
    NotificationChannel,
    NotificationLevel,
    NotificationService,
)
from backend.engines.scheduler import ScheduledTask, Scheduler

logger = logging.getLogger(__name__)

#: Action types a scheduled task or monitor trigger may run.
KNOWN_ACTION_TYPES = frozenset({"notify", "agent_turn", "reactor"})

_LEVEL_MAP = {m.value: m for m in NotificationLevel}
_CHANNEL_MAP = {m.value: m for m in NotificationChannel}


# ---------------------------------------------------------------------------
# Session factory indirection (tests point this at their temp database)
# ---------------------------------------------------------------------------


def _default_session_factory():
    from backend.database.base import get_sessionmaker

    return get_sessionmaker()


_session_factory = _default_session_factory


def set_session_factory(factory) -> None:
    """Swap the process DB factory used by background work (tests)."""
    global _session_factory
    _session_factory = factory


def reset_proactive_runtime() -> None:
    """Clear shared runtime state and restore the default DB factory.

    Called between tests so proactive state never leaks across cases. The
    scheduler/monitor instances themselves are kept (routers hold references to
    them); only their in-memory state is dropped.
    """
    global _session_factory
    _session_factory = _default_session_factory
    get_scheduler().clear()
    get_event_monitor().clear()


# ---------------------------------------------------------------------------
# Host metrics (portable, stdlib-only)
# ---------------------------------------------------------------------------

_cpu_sample: tuple[float, float, float] | None = None  # (monotonic, user, system)


def sample_cpu_usage() -> float:
    """Return CPU busy percentage since the previous call (```os.times``).

    Portable across platforms; the first call returns 0.0 (no baseline).
    """
    global _cpu_sample
    t = time.monotonic()
    user, system = os.times()[:2]
    previous = _cpu_sample
    _cpu_sample = (t, user, system)
    if previous is None:
        return 0.0
    elapsed = t - previous[0]
    busy = (user - previous[1]) + (system - previous[2])
    if elapsed <= 0:
        return 0.0
    return min(100.0, max(0.0, busy / elapsed * 100.0))


def sample_memory_percent() -> float | None:
    """Return system-wide memory usage percent where supported, else ``None``."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _MemoryStatusEx(ctypes.Structure):  # noqa: N801 - win32 struct
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return float(status.dwMemoryLoad)
        except Exception:  # noqa: BLE001 - metrics are best-effort
            logger.debug("Memory metrics unavailable on this host", exc_info=True)
    return None


async def _cpu_usage_source() -> float:
    return sample_cpu_usage()


async def _memory_percent_source() -> float | None:
    return sample_memory_percent()


async def _unread_count_source() -> int:
    factory = _session_factory()
    async with factory() as session:
        return await get_notification_service().unread_count(session)


async def _facts_count_source() -> int:
    """Total number of stored long-term facts."""
    from sqlalchemy import func, select

    from backend.models.fact import Fact

    factory = _session_factory()
    async with factory() as session:
        total = await session.scalar(select(func.count(Fact.id)))
        return int(total or 0)


async def _facts_text_source() -> str:
    """All facts formatted as lines ('key: value'), for pattern triggers."""
    from backend.services.fact_service import FactService

    factory = _session_factory()
    async with factory() as session:
        lines = await FactService().all_as_text(session, limit=200)
        return "\n".join(lines)


async def _conversations_new_24h_source() -> int:
    """Conversations created in the last 24 hours."""
    from sqlalchemy import func, select

    from backend.models.conversation import Conversation

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Conversation.id)).where(Conversation.created_at >= cutoff)
        )
        return int(total or 0)


async def _conversations_messages_24h_source() -> int:
    """Messages created in the last 24 hours (a conversation-activity measure)."""
    from sqlalchemy import func, select

    from backend.models.message import Message

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Message.id)).where(Message.created_at >= cutoff)
        )
        return int(total or 0)


# ---------------------------------------------------------------------------
# Scoped sources (trigger reference: "<base>:<scope>")
# ---------------------------------------------------------------------------


async def _facts_count_category_source(category: str) -> int:
    """Count of stored facts in ``category``."""
    from sqlalchemy import func, select

    from backend.models.fact import Fact

    factory = _session_factory()
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Fact.id)).where(Fact.category == category)
        )
        return int(total or 0)


async def _facts_project_count_source(project_id: str) -> int:
    """Count of stored facts scoped to ``project_id``."""
    from sqlalchemy import func, select

    from backend.models.fact import Fact

    factory = _session_factory()
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Fact.id)).where(Fact.project_id == project_id)
        )
        return int(total or 0)


async def _facts_text_category_source(category: str) -> str:
    """Facts of ``category`` formatted as lines, for pattern triggers."""
    from backend.services.fact_service import FactService

    factory = _session_factory()
    async with factory() as session:
        lines = await FactService().all_as_text(
            session, category=category, limit=200
        )
        return "\n".join(lines)


async def _facts_project_text_source(project_id: str) -> str:
    """Facts scoped to ``project_id`` formatted as lines, for pattern triggers."""
    from backend.services.fact_service import FactService

    factory = _session_factory()
    async with factory() as session:
        lines = await FactService().all_as_text(
            session, project_id=project_id, limit=200
        )
        return "\n".join(lines)


async def _conversations_project_new_24h_source(project_id: str) -> int:
    """Conversations created in the last 24 hours within ``project_id``."""
    from sqlalchemy import func, select

    from backend.models.conversation import Conversation

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.project_id == project_id,
                Conversation.created_at >= cutoff,
            )
        )
        return int(total or 0)


async def _conversations_messages_24h_scoped_source(conversation_id: str) -> int:
    """Messages created in the last 24 hours within one conversation."""
    from sqlalchemy import func, select

    from backend.models.message import Message

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Message.id)).where(
                Message.conversation_id == conversation_id,
                Message.created_at >= cutoff,
            )
        )
        return int(total or 0)


async def _conversations_project_messages_24h_source(project_id: str) -> int:
    """Messages created in the last 24 hours across a project's conversations."""
    from sqlalchemy import func, select

    from backend.models.conversation import Conversation
    from backend.models.message import Message

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.project_id == project_id,
                Message.created_at >= cutoff,
            )
        )
        return int(total or 0)


# ---------------------------------------------------------------------------
# Cross-project aggregate sources (volume/velocity across every project)
# ---------------------------------------------------------------------------


async def _projects_count_source() -> int:
    """Total number of projects."""
    from sqlalchemy import func, select

    from backend.models.project import Project

    factory = _session_factory()
    async with factory() as session:
        total = await session.scalar(select(func.count(Project.id)))
        return int(total or 0)


async def _projects_new_24h_source() -> int:
    """Projects created in the last 24 hours."""
    from sqlalchemy import func, select

    from backend.models.project import Project

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Project.id)).where(Project.created_at >= cutoff)
        )
        return int(total or 0)


async def _projects_messages_24h_source() -> int:
    """Messages created in the last 24 hours inside any project conversation."""
    from sqlalchemy import func, select

    from backend.models.conversation import Conversation
    from backend.models.message import Message

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.project_id.is_not(None),
                Message.created_at >= cutoff,
            )
        )
        return int(total or 0)


async def _projects_active_24h_source() -> int:
    """Distinct projects with at least one message in the last 24 hours."""
    from sqlalchemy import func, select

    from backend.models.conversation import Conversation
    from backend.models.message import Message

    factory = _session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with factory() as session:
        total = await session.scalar(
            select(func.count(func.distinct(Conversation.project_id)))
            .join(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.project_id.is_not(None),
                Message.created_at >= cutoff,
            )
        )
        return int(total or 0)


# ---------------------------------------------------------------------------
# History sources (triggers react to their own / another task's track record)
# ---------------------------------------------------------------------------


async def _history_task_failure_rate_source(task_id: str) -> float:
    """Failure rate (0.0-1.0) of a scheduled task's run history."""
    from backend.services.proactive_store import task_failure_rate

    factory = _session_factory()
    async with factory() as session:
        return await task_failure_rate(session, task_id)


async def _history_task_total_runs_source(task_id: str) -> int:
    """Total run count of a scheduled task."""
    from backend.services.proactive_store import count_task_runs

    factory = _session_factory()
    async with factory() as session:
        return await count_task_runs(session, task_id)


async def _history_task_last_error_source(task_id: str) -> str:
    """Most recent failure message of a scheduled task ("" if none)."""
    from backend.services.proactive_store import task_last_error

    factory = _session_factory()
    async with factory() as session:
        return (await task_last_error(session, task_id)) or ""


async def _history_task_error_groups_source(task_id: str) -> int:
    """Number of distinct error messages in a scheduled task's run history."""
    from backend.services.proactive_store import count_task_error_groups

    factory = _session_factory()
    async with factory() as session:
        return await count_task_error_groups(session, task_id)


async def _history_trigger_fire_count_source(trigger_id: str) -> int:
    """Total fire count of a monitor trigger."""
    from backend.services.proactive_store import count_trigger_fires

    factory = _session_factory()
    async with factory() as session:
        return await count_trigger_fires(session, trigger_id)


async def _history_trigger_last_value_source(trigger_id: str) -> Any:
    """Most recent satisfying value recorded for a monitor trigger."""
    from backend.services.proactive_store import trigger_last_value

    factory = _session_factory()
    async with factory() as session:
        return await trigger_last_value(session, trigger_id)


# ---------------------------------------------------------------------------
# Notification emission (persist + broadcast)
# ---------------------------------------------------------------------------


@lru_cache
def get_notification_service() -> NotificationService:
    """Return the shared database-backed notification inbox."""
    from backend.services.notification_service import NotificationService

    return NotificationService()


async def _persist_and_push(
    *,
    title: str,
    message: str,
    level: str | NotificationLevel = NotificationLevel.INFO,
    channel: str | NotificationChannel = NotificationChannel.IN_APP,
    agent: str | None = None,
    payload: dict | None = None,
) -> None:
    """Persist a notification and broadcast it. Never raises on failure."""
    level_value = _LEVEL_MAP[level.value if isinstance(level, NotificationLevel) else level].value
    channel_value = (
        channel.value
        if isinstance(channel, NotificationChannel)
        else channel
    )
    factory = _session_factory()
    try:
        async with factory() as session:
            service = get_notification_service()
            record = await service.create(
                session,
                title=title,
                message=message,
                level=level_value,
                channel=channel_value,
                agent=agent,
                payload=payload or {},
            )
            await session.commit()
            await service.push(record)
    except Exception:  # noqa: BLE001 - proactive alerts must never crash tasks
        logger.exception("Failed to persist notification '%s'", title)


async def _monitor_sink(notification: Notification) -> None:
    """EventMonitor hook: persist + broadcast a fired trigger alert."""
    await _persist_and_push(
        title=notification.title,
        message=notification.message,
        level=notification.level,
        channel=notification.channel,
        agent=notification.agent,
        payload=notification.metadata or {},
    )


# ---------------------------------------------------------------------------
# Durable state (tasks/triggers persisted to the database)
# ---------------------------------------------------------------------------


async def _persist_task_state(task: ScheduledTask) -> None:
    """Engine state hook: persist a scheduled task's snapshot. Never raises."""
    from backend.services.proactive_store import save_scheduled_task

    factory = _session_factory()
    try:
        async with factory() as session:
            await save_scheduled_task(session, task)
            await session.commit()
    except Exception:  # noqa: BLE001 - a persist failure must not break the loop
        logger.exception("Failed to persist scheduled task '%s'", task.id)


async def _persist_trigger_state(trigger: Trigger) -> None:
    """Engine state hook: persist a monitor trigger's snapshot. Never raises."""
    from backend.services.proactive_store import save_monitor_trigger

    factory = _session_factory()
    try:
        async with factory() as session:
            await save_monitor_trigger(session, trigger)
            await session.commit()
    except Exception:  # noqa: BLE001 - a persist failure must not break the loop
        logger.exception("Failed to persist monitor trigger '%s'", trigger.id)


async def _record_task_run(task: ScheduledTask, duration_ms: float) -> None:
    """Scheduler run hook: append one run-history row. Never raises."""
    from backend.services.proactive_store import record_task_run

    factory = _session_factory()
    try:
        async with factory() as session:
            await record_task_run(
                session,
                task_id=task.id,
                run_at=task.last_run,
                status=task.status.value,
                duration_ms=round(duration_ms, 3),
                run_count=task.run_count,
                error=task.last_error,
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - a history failure must not crash the task
        logger.exception("Failed to record run history for task '%s'", task.id)

    # Reactor notifications: stream this run's result/error groups to the inbox
    # for any reactor task listening to this task id. Never raises.
    await _emit_reactor_alerts(task, duration_ms)


async def _emit_reactor_alerts(task: ScheduledTask, duration_ms: float) -> None:
    """Stream a task's run result to reactor tasks listening to it.

    A reactor task (handler ``reactor``) declares ``payload`` with:
    * ``listen_task_id`` — the source task whose runs it reacts to;
    * ``on`` — ``"all"`` | ``"success"`` | ``"failure"`` (default ``"all"``);
    * ``message`` — optional custom message override.

    When the source task produces a run outcome matching ``on``, a notification
    is emitted carrying the run's status, duration, error, and the source
    task's distinct error-group count — streaming run logs to the inbox rather
    than only the summary. Never raises.
    """
    if not task.last_run:
        return
    status = task.status.value
    if status not in ("completed", "failed"):
        return

    listeners = [
        t for t in get_scheduler().list_tasks()
        if (t.handler or t.name) == "reactor"
        and t.payload.get("listen_task_id") == task.id
    ]
    if not listeners:
        return

    from backend.services.proactive_store import count_task_error_groups

    factory = _session_factory()
    error_groups = 0
    try:
        async with factory() as session:
            error_groups = await count_task_error_groups(session, task.id)
    except Exception:  # noqa: BLE001 - best-effort enrichment
        logger.exception("Failed to count error groups for reactor on '%s'", task.id)

    outcome_payload = {
        "origin": "reactor",
        "listened_task_id": task.id,
        "status": status,
        "duration_ms": round(duration_ms, 3),
        "run_count": task.run_count,
        "error": task.last_error,
        "error_groups": error_groups,
    }

    for listener in listeners:
        on = str(listener.payload.get("on", "all"))
        if on == "success" and status != "completed":
            continue
        if on == "failure" and status != "failed":
            continue

        default_message = (
            f"Task '{task.name}' finished with status '{status}' "
            f"in {round(duration_ms, 1)}ms "
            f"({error_groups} distinct error group(s))."
        )
        await _persist_and_push(
            title=f"Run result: {task.name} ({status})",
            message=str(listener.payload.get("message") or default_message),
            level=(
                NotificationLevel.ERROR
                if status == "failed"
                else NotificationLevel.SUCCESS
            ),
            agent=listener.agent or "beru_core",
            payload=outcome_payload,
        )


async def _record_trigger_fire(trigger: Trigger, value: Any) -> None:
    """EventMonitor fire hook: append one fire-history row. Never raises."""
    from backend.services.proactive_store import record_trigger_fire

    condition = {
        "condition_type": trigger.condition.condition_type.value,
        "source": trigger.condition.source,
        "operator": trigger.condition.operator,
        "value": trigger.condition.value,
        "params": trigger.condition.params or {},
    }
    factory = _session_factory()
    try:
        async with factory() as session:
            await record_trigger_fire(
                session,
                trigger_id=trigger.id,
                fired_at=trigger.last_fired,
                condition=condition,
                value=value,
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - a history failure must not break the loop
        logger.exception("Failed to record fire history for trigger '%s'", trigger.id)


async def prune_old_history() -> int:
    """Delete audit history per-source retention policies.

    Each task and trigger can declare its own ``retention_days`` / ``keep_last``.
    When a source has no per-source policy (fields are ``None``), the global
    ``BERU_PROACTIVE_AUDIT_RETENTION_DAYS`` default applies. A global value of
    0 disables the time-based default but does **not** block per-source prunes.
    Runs once at startup. Never raises; returns the number of rows removed.
    """
    from backend.core.config import get_settings

    days = get_settings().proactive_audit_retention_days
    global_retention = float(days) * 86400 if days and days > 0 else 0.0

    from backend.services.proactive_store import (
        prune_task_runs,
        prune_trigger_fires,
    )

    factory = _session_factory()
    removed = 0
    try:
        async with factory() as session:
            for task in list(get_scheduler().list_tasks()):
                t_ret = (
                    float(task.retention_days) * 86400
                    if task.retention_days and task.retention_days > 0
                    else global_retention or None
                )
                removed += await prune_task_runs(
                    session,
                    task.id,
                    retention_seconds=t_ret,
                    keep_last=task.keep_last,
                )
            for trigger in list(get_event_monitor().list_triggers()):
                t_ret = (
                    float(trigger.retention_days) * 86400
                    if trigger.retention_days and trigger.retention_days > 0
                    else global_retention or None
                )
                removed += await prune_trigger_fires(
                    session,
                    trigger.id,
                    retention_seconds=t_ret,
                    keep_last=trigger.keep_last,
                )
            await session.commit()
    except Exception:  # noqa: BLE001 - retention must never block startup
        logger.exception("Failed to prune audit history")
        return 0
    if removed:
        logger.info(
            "Pruned %d audit row(s) (global default %.1f day(s))", removed, days,
        )
    return removed


async def restore_proactive_state() -> None:
    """Reload persisted scheduled tasks and monitor triggers into the engines.

    Idempotent: records already present in an engine (same id) are skipped, so
    calling this after a previous restore or while tasks were added in-process
    does not duplicate anything. Invoked at startup so provisioned behaviour
    survives restarts.
    """
    from backend.services.proactive_store import (
        load_monitor_triggers,
        load_scheduled_tasks,
    )

    factory = _session_factory()
    try:
        async with factory() as session:
            tasks = await load_scheduled_tasks(session)
            triggers = await load_monitor_triggers(session)
    except Exception:  # noqa: BLE001 - startup must not die on a bad store row
        logger.exception("Failed to restore proactive state from storage")
        return

    scheduler = get_scheduler()
    for task in tasks:
        if scheduler.get_task(task.id) is None:
            scheduler.add_task(task)

    monitor = get_event_monitor()
    for trigger in triggers:
        if monitor.get_trigger(trigger.id) is None:
            monitor.add_trigger(trigger)

    logger.info(
        "Restored %d scheduled task(s) and %d monitor trigger(s) from storage",
        len(tasks),
        len(triggers),
    )


# ---------------------------------------------------------------------------
# Action dispatch (shared by scheduled tasks and monitor trigger actions)
# ---------------------------------------------------------------------------


async def _dispatch_action(action: dict, origin: str) -> None:
    """Run one action dict ``{"type": "notify"|"agent_turn"|"reactor", ...}``."""
    action_type = action.get("type")
    if action_type == "notify":
        await _do_notify(action, origin)
    elif action_type == "agent_turn":
        await _do_agent_turn(action, origin)
    elif action_type == "reactor":
        # A reactor task is a passive listener: it reacts to another task's run
        # results via the scheduler run hook. Running it standalone is a no-op
        # (there is nothing for it to listen to without a source run).
        logger.debug("Reactor task evaluated (origin=%s)", origin)
    else:
        logger.warning("Unknown action type %r from %s", action_type, origin)


async def _do_notify(action: dict, origin: str) -> None:
    title = str(action.get("title", "")).strip()
    message = str(action.get("message", "")).strip()
    if not title and not message:
        logger.warning("notify action from %s has no title/message; skipped", origin)
        return
    await _persist_and_push(
        title=title or "BERU notification",
        message=message,
        level=action.get("level", "info"),
        channel=action.get("channel", "in_app"),
        agent=action.get("agent"),
        payload={"origin": origin, **(action.get("payload") or {})},
    )


async def _do_agent_turn(action: dict, origin: str) -> None:
    """Run one agent turn through ChatService (confirmation-safe).

    Pending confirmations (sensitive tool calls) are surfaced as alerts for the
    user to approve via ``POST /api/v1/chat/confirm``; they are never silently
    auto-executed.
    """
    from backend.api.deps import get_chat_service
    from backend.core.config import get_settings
    from backend.engines.intelligence import get_intelligence_engine
    from backend.schemas.chat import ChatRequest

    message = str(action.get("message", "")).strip()
    if not message:
        logger.warning("agent_turn action from %s has no message; skipped", origin)
        return

    agent = action.get("agent")
    conversation_id = action.get("conversation_id") or None
    request = ChatRequest(
        message=message,
        agent=agent,
        conversation_id=conversation_id,
    )

    factory = _session_factory()
    try:
        async with factory() as session:
            service = get_chat_service(get_intelligence_engine())
            outcome = await service.process(session, get_settings(), request)

        conv_id = outcome.conversation.id
        agent_name = agent or outcome.conversation.agent
        snippet = outcome.result.content[:200].replace("\n", " ")
        await _persist_and_push(
            title=f"Scheduled turn completed ({agent_name})",
            message=(
                f"{agent_name} replied in conversation {conv_id}: {snippet}"
            ),
            level=NotificationLevel.INFO,
            agent=agent_name,
            payload={"origin": origin, "conversation_id": conv_id},
        )

        for pending in outcome.pending_confirmations or []:
            tool = pending.get("tool", "tool")
            confirmation_id = pending.get("confirmation_id", "")
            await _persist_and_push(
                title=f"Approval required: {tool}",
                message=(
                    f"A scheduled turn wants to run '{tool}'. Approve via "
                    f"POST /api/v1/chat/confirm (conversation_id={conv_id}, "
                    f"confirmation_id={confirmation_id})."
                ),
                level=NotificationLevel.WARNING,
                agent=agent_name,
                payload={"conversation_id": conv_id, "confirmation_id": confirmation_id},
            )
    except Exception:  # noqa: BLE001 - surface the outcome, don't crash the loop
        logger.exception("Scheduled agent turn failed (origin=%s)", origin)
        await _persist_and_push(
            title="Scheduled turn failed",
            message=(
                "A scheduled agent turn did not complete. Check the BERU logs "
                "for details."
            ),
            level=NotificationLevel.ERROR,
            agent=agent,
            payload={"origin": origin},
        )


async def _run_trigger_actions(trigger: Trigger) -> None:
    """EventMonitor hook: dispatch every action configured on a trigger."""
    for action in trigger.actions or []:
        await _dispatch_action(action, origin=f"trigger:{trigger.name}")


# ---------------------------------------------------------------------------
# Scheduler / monitor singletons
# ---------------------------------------------------------------------------


async def _task_agent_turn(task: ScheduledTask) -> None:
    await _dispatch_action({"type": task.handler, **task.payload}, origin=f"task:{task.name}")


async def _task_notify(task: ScheduledTask) -> None:
    await _dispatch_action({"type": task.handler, **task.payload}, origin=f"task:{task.name}")


async def _task_reactor(task: ScheduledTask) -> None:
    # A reactor task is a passive listener: it reacts to another task's run
    # results via the scheduler run hook. Running it standalone does nothing.
    logger.debug("Reactor task '%s' is a listener; standalone run is a no-op", task.name)


def _build_scheduler() -> Scheduler:
    scheduler = Scheduler()
    scheduler.register_handler("agent_turn", _task_agent_turn)
    scheduler.register_handler("notify", _task_notify)
    scheduler.register_handler("reactor", _task_reactor)
    scheduler.set_state_hook(_persist_task_state)
    scheduler.set_run_hook(_record_task_run)
    return scheduler


def _build_monitor() -> EventMonitor:
    monitor = EventMonitor(NotificationService())
    monitor.set_sink(_monitor_sink)
    monitor.set_action_runner(_run_trigger_actions)
    monitor.set_state_hook(_persist_trigger_state)
    monitor.set_fire_hook(_record_trigger_fire)
    monitor.register_source("system.cpu_usage", _cpu_usage_source)
    monitor.register_source("system.memory_percent", _memory_percent_source)
    monitor.register_source("notifications.unread", _unread_count_source)
    monitor.register_source("memory.facts_count", _facts_count_source)
    monitor.register_scoped_source("memory.facts_count", _facts_count_category_source)
    monitor.register_scoped_source(
        "memory.facts_project_count", _facts_project_count_source
    )
    monitor.register_source("memory.facts_text", _facts_text_source)
    monitor.register_scoped_source("memory.facts_text", _facts_text_category_source)
    monitor.register_scoped_source(
        "memory.facts_project_text", _facts_project_text_source
    )
    monitor.register_source("conversations.new_24h", _conversations_new_24h_source)
    monitor.register_scoped_source(
        "conversations.new_24h", _conversations_project_new_24h_source
    )
    monitor.register_source(
        "conversations.messages_24h", _conversations_messages_24h_source
    )
    monitor.register_scoped_source(
        "conversations.messages_24h", _conversations_messages_24h_scoped_source
    )
    monitor.register_scoped_source(
        "conversations.project_messages_24h", _conversations_project_messages_24h_source
    )
    monitor.register_source("projects.count", _projects_count_source)
    monitor.register_source("projects.new_24h", _projects_new_24h_source)
    monitor.register_source("projects.messages_24h", _projects_messages_24h_source)
    monitor.register_source("projects.active_24h", _projects_active_24h_source)
    monitor.register_scoped_source(
        "history.task_failure_rate", _history_task_failure_rate_source
    )
    monitor.register_scoped_source(
        "history.task_total_runs", _history_task_total_runs_source
    )
    monitor.register_scoped_source(
        "history.task_last_error", _history_task_last_error_source
    )
    monitor.register_scoped_source(
        "history.task_error_groups", _history_task_error_groups_source
    )
    monitor.register_scoped_source(
        "history.trigger_fire_count", _history_trigger_fire_count_source
    )
    monitor.register_scoped_source(
        "history.trigger_last_value", _history_trigger_last_value_source
    )
    return monitor


@lru_cache
def get_scheduler() -> Scheduler:
    """Return the process-wide scheduler with the action handlers registered."""
    return _build_scheduler()


@lru_cache
def get_event_monitor() -> EventMonitor:
    """Return the process-wide monitor with real data sources + sinks wired."""
    return _build_monitor()


# ---------------------------------------------------------------------------
# Lifespan control
# ---------------------------------------------------------------------------


async def start_proactive_runtime() -> None:
    """Restore persisted state, prune retained history, then start the loops."""
    scheduler = get_scheduler()
    monitor = get_event_monitor()
    if scheduler.running or monitor.running:
        return
    await restore_proactive_state()
    await prune_old_history()
    await scheduler.start()
    await monitor.start()
    logger.info("Proactive runtime started (scheduler + monitor)")


async def stop_proactive_runtime() -> None:
    """Stop the scheduler and monitor loops."""
    await get_scheduler().stop()
    await get_event_monitor().stop()
    logger.info("Proactive runtime stopped")