"""Scheduler engine — manages recurring and one-shot background tasks.

Supports cron-like scheduling, interval-based tasks, and one-shot delayed tasks.
Tasks are defined as callables that can trigger agent actions, notifications, or
arbitrary background work.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    ONE_SHOT = "one_shot"
    INTERVAL = "interval"
    CRON = "cron"


@dataclass
class ScheduledTask:
    """A scheduled background task."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    description: str = ""
    # Handler key looked up in the scheduler's registered handlers. Defaults to
    # the task ``name`` when empty, preserving the compatibility contract.
    handler: str = ""
    task_type: TaskType = TaskType.ONE_SHOT
    status: TaskStatus = TaskStatus.PENDING

    # Scheduling parameters
    interval_seconds: float | None = None
    run_at: datetime | None = None
    cron_expr: str | None = None

    # Execution state
    last_run: datetime | None = None
    next_run: datetime | None = None
    run_count: int = 0
    max_runs: int | None = None
    last_error: str | None = None

    # Payload
    payload: dict[str, Any] = field(default_factory=dict)
    agent: str | None = None

    # Per-source audit retention policy (None = use the global default).
    retention_days: float | None = None
    keep_last: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "handler": self.handler,
            "task_type": self.task_type.value,
            "status": self.status.value,
            "interval_seconds": self.interval_seconds,
            "run_at": self.run_at.isoformat() if self.run_at else None,
            "cron_expr": self.cron_expr,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "run_count": self.run_count,
            "max_runs": self.max_runs,
            "last_error": self.last_error,
            "payload": self.payload,
            "agent": self.agent,
            "retention_days": self.retention_days,
            "keep_last": self.keep_last,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledTask:
        run_at = data.get("run_at")
        if run_at and isinstance(run_at, str):
            run_at = datetime.fromisoformat(run_at)
        last_run = data.get("last_run")
        if last_run and isinstance(last_run, str):
            last_run = datetime.fromisoformat(last_run)
        next_run = data.get("next_run")
        if next_run and isinstance(next_run, str):
            next_run = datetime.fromisoformat(next_run)

        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            name=data.get("name", ""),
            description=data.get("description", ""),
            handler=data.get("handler", ""),
            task_type=TaskType(data.get("task_type", "one_shot")),
            status=TaskStatus(data.get("status", "pending")),
            interval_seconds=data.get("interval_seconds"),
            run_at=run_at,
            cron_expr=data.get("cron_expr"),
            last_run=last_run,
            next_run=next_run,
            run_count=data.get("run_count", 0),
            max_runs=data.get("max_runs"),
            last_error=data.get("last_error"),
            payload=data.get("payload", {}),
            agent=data.get("agent"),
            retention_days=data.get("retention_days"),
            keep_last=data.get("keep_last"),
        )


class Scheduler:
    """Background task scheduler that runs in the FastAPI lifespan."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._handlers: dict[str, Callable[..., Awaitable[Any]]] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        # Optional async hook called with a task after any execution changes its
        # state/counters (run_due / run_task). Used to persist task state.
        self._state_hook: Callable[[ScheduledTask], Awaitable[None]] | None = None
        # Optional async hook called after every execution with the task and its
        # wall-clock duration in milliseconds. Used to append run history.
        self._run_hook: Callable[[ScheduledTask, float], Awaitable[None]] | None = None

    @property
    def running(self) -> bool:
        """Whether the background scheduler loop is currently running."""
        return self._running

    def set_state_hook(self, hook: Callable[[ScheduledTask], Awaitable[None]]) -> None:
        """Install an async hook invoked after a task's state changes.

        The hook receives the finished task snapshot (status, counters, next
        run). It is awaited after execution; a raising hook is surfaced to the
        caller of :meth:`run_due` / :meth:`run_task`. Optional — the scheduler
        works standalone without one.
        """
        self._state_hook = hook

    async def _save_state(self, task: ScheduledTask) -> None:
        if self._state_hook is not None:
            await self._state_hook(task)

    def set_run_hook(
        self, hook: Callable[[ScheduledTask, float], Awaitable[None]]
    ) -> None:
        """Install an async hook invoked after every task execution.

        The hook receives the finished task snapshot (status, counters, error)
        and the handler's wall-clock duration in milliseconds. It is awaited for
        both successful and failed runs. Optional — the scheduler works
        standalone without one.
        """
        self._run_hook = hook

    async def _record_run(self, task: ScheduledTask, duration_ms: float) -> None:
        if self._run_hook is not None:
            await self._run_hook(task, duration_ms)

    def register_handler(self, name: str, handler: Callable[..., Awaitable[Any]]) -> None:
        """Register a handler function for a task type."""
        self._handlers[name] = handler

    def add_task(self, task: ScheduledTask) -> ScheduledTask:
        """Add a scheduled task."""
        if task.next_run is None and task.run_at:
            task.next_run = task.run_at
        elif task.next_run is None and task.interval_seconds:
            task.next_run = datetime.now(timezone.utc) + timedelta(
                seconds=task.interval_seconds
            )

        self._tasks[task.id] = task
        logger.info("Scheduled task '%s' (%s)", task.name, task.id)
        return task

    def get_task(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def list_active_tasks(self) -> list[ScheduledTask]:
        return [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        ]

    def pause_task(self, task_id: str) -> ScheduledTask | None:
        task = self._tasks.get(task_id)
        if task and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            task.status = TaskStatus.PAUSED
            return task
        return None

    def resume_task(self, task_id: str) -> ScheduledTask | None:
        task = self._tasks.get(task_id)
        if task and task.status == TaskStatus.PAUSED:
            task.status = TaskStatus.PENDING
            task.next_run = datetime.now(timezone.utc)
            return task
        return None

    def cancel_task(self, task_id: str) -> ScheduledTask | None:
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.CANCELLED
            return task
        return None

    def delete_task(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            return True
        return False

    def clear(self) -> int:
        """Remove every scheduled task. Returns the number removed."""
        count = len(self._tasks)
        self._tasks.clear()
        return count

    async def _execute_task(self, task: ScheduledTask) -> None:
        """Execute a single task."""
        task.status = TaskStatus.RUNNING
        task.last_run = datetime.now(timezone.utc)
        started = time.monotonic()

        try:
            handler = self._handlers.get(task.handler or task.name)
            if handler:
                await handler(task)
            else:
                logger.warning("No handler for task '%s'", task.name)

            task.status = TaskStatus.COMPLETED
            task.run_count += 1

            # Check if we've hit max runs
            if task.max_runs and task.run_count >= task.max_runs:
                task.status = TaskStatus.COMPLETED
            elif task.task_type == TaskType.ONE_SHOT:
                task.status = TaskStatus.COMPLETED
            else:
                task.status = TaskStatus.PENDING

        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            logger.exception("Task '%s' failed", task.name)

        duration_ms = (time.monotonic() - started) * 1000.0
        # Append run history (success or failure) after the state is final.
        await self._record_run(task, duration_ms)

    async def run_due(self) -> list[ScheduledTask]:
        """Execute every pending task whose ``next_run`` has arrived.

        Returns the tasks that ran this pass. Handlers may also be driven
        explicitly via :meth:`run_task` (e.g. a manual "run now" trigger or a
        single tick in tests).
        """
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        ran: list[ScheduledTask] = []

        for task in list(self._tasks.values()):
            if task.status != TaskStatus.PENDING:
                continue
            if task.next_run is None:
                continue

            next_ts = (
                task.next_run.timestamp()
                if isinstance(task.next_run, datetime)
                else task.next_run
            )

            if next_ts <= now_ts:
                await self._execute_task(task)
                ran.append(task)

                # Calculate next run
                if task.status == TaskStatus.PENDING and task.interval_seconds:
                    task.next_run = now + timedelta(seconds=task.interval_seconds)

                # Persist the finished snapshot (status, counters, next run).
                await self._save_state(task)

        return ran

    async def run_task(self, task_id: str) -> ScheduledTask | None:
        """Execute a single task now, regardless of schedule. ``None`` if unknown."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        await self._execute_task(task)
        await self._save_state(task)
        return task

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop that checks and runs due tasks."""
        while self._running:
            self._wake.clear()
            await self.run_due()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        """Start the scheduler loop."""
        if self._running:
            return
        self._running = True
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler loop gracefully (no work cancelled mid-session)."""
        self._running = False
        if self._wake:
            self._wake.set()
        if self._task:
            await self._task
            self._task = None
        logger.info("Scheduler stopped")
