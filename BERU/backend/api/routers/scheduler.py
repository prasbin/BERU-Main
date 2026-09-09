"""Scheduled tasks and notifications API endpoints.

Uses the shared proactive runtime singletons (``scheduler`` and the
database-backed notification inbox) shared with the monitor router and the app
lifespan, so the API, background task execution, and monitor triggers observe
the same state.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.security import require_api_key
from backend.database.base import get_session
from backend.engines.notifications import NotificationChannel, NotificationLevel
from backend.services.proactive_service import (
    KNOWN_ACTION_TYPES,
    get_notification_service,
    get_scheduler,
)
from backend.services.proactive_store import (
    delete_scheduled_task,
    save_scheduled_task,
)

router = APIRouter(prefix="/scheduler", tags=["scheduler"], dependencies=[Depends(require_api_key)])

_LEVEL_MAP = {m.value: m for m in NotificationLevel}
_CHANNEL_MAP = {m.value: m for m in NotificationChannel}

_scheduler = get_scheduler()
_notification_service = get_notification_service()


# ---- Request/Response models ----

class TaskCreateRequest(BaseModel):
    name: str
    description: str = ""
    # Which handler runs the task: "notify" | "agent_turn".
    handler: str = "notify"
    task_type: str = "interval"
    interval_seconds: float | None = None
    run_at: str | None = None
    max_runs: int | None = None
    agent: str | None = None
    payload: dict = {}
    retention_days: float | None = None
    keep_last: int | None = None


class TaskUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    interval_seconds: float | None = None
    max_runs: int | None = None
    retention_days: float | None = None
    keep_last: int | None = None


class NotificationCreateRequest(BaseModel):
    title: str
    message: str
    level: str = "info"
    channel: str = "in_app"
    agent: str | None = None
    payload: dict = {}


def _parse_member(mapping: dict, value: str, label: str):
    try:
        return mapping[value]
    except KeyError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown {label} '{value}'. Valid values: {sorted(mapping)}",
        ) from None


def _parse_task_type(value: str):
    """Validate ``task_type``, returning the enum member (422 on bad values)."""
    from backend.engines.scheduler import TaskType

    return _parse_member({m.value: m for m in TaskType}, value, "task_type")


# ---- Task endpoints ----

@router.post(
    "/tasks",
    status_code=status.HTTP_201_CREATED,
    summary="Create a scheduled task",
)
async def create_task(
    body: TaskCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from datetime import datetime

    handler = body.handler or "notify"
    if handler not in KNOWN_ACTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown handler '{handler}'. Valid handlers: "
                f"{sorted(KNOWN_ACTION_TYPES)}"
            ),
        )

    run_at = None
    if body.run_at:
        try:
            run_at = datetime.fromisoformat(body.run_at)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="run_at must be an ISO-8601 datetime"
            ) from None

    from backend.engines.scheduler import ScheduledTask

    task = ScheduledTask(
        name=body.name,
        description=body.description,
        handler=handler,
        task_type=_parse_task_type(body.task_type),
        interval_seconds=body.interval_seconds,
        run_at=run_at,
        max_runs=body.max_runs,
        agent=body.agent,
        payload=body.payload,
        retention_days=body.retention_days,
        keep_last=body.keep_last,
    )
    _scheduler.add_task(task)
    # Persist so the task survives restarts (and restore keeps it in sync).
    await save_scheduled_task(session, task)
    await session.commit()
    return task.to_dict()


@router.get("/tasks", summary="List all scheduled tasks")
async def list_tasks() -> list[dict]:
    return [t.to_dict() for t in _scheduler.list_tasks()]


@router.get("/tasks/active", summary="List active tasks")
async def list_active_tasks() -> list[dict]:
    return [t.to_dict() for t in _scheduler.list_active_tasks()]


@router.get("/tasks/{task_id}", summary="Get a task")
async def get_task(task_id: str) -> dict:
    task = _scheduler.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task.to_dict()


@router.patch("/tasks/{task_id}", summary="Update a scheduled task")
async def update_task(
    task_id: str,
    body: TaskUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    task = _scheduler.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    if body.name is not None:
        task.name = body.name
    if body.description is not None:
        task.description = body.description
    if body.interval_seconds is not None:
        task.interval_seconds = body.interval_seconds
    if body.max_runs is not None:
        task.max_runs = body.max_runs
    # Explicit None sentinel distinguishes "clear the field" from "don't change".
    if body.retention_days is not None or "retention_days" in body.model_fields_set:
        task.retention_days = body.retention_days
    if body.keep_last is not None or "keep_last" in body.model_fields_set:
        task.keep_last = body.keep_last
    await save_scheduled_task(session, task)
    await session.commit()
    return task.to_dict()


@router.get("/tasks/{task_id}/runs", summary="Get a task's run history")
async def task_runs(
    task_id: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not _scheduler.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    from backend.services.proactive_store import (
        count_task_runs,
        list_task_runs,
        task_run_summary,
        task_run_to_dict,
    )

    rows = await list_task_runs(session, task_id, limit=min(max(limit, 1), 500))
    return {
        "task_id": task_id,
        "total": await count_task_runs(session, task_id),
        "summary": await task_run_summary(session, task_id),
        "runs": [task_run_to_dict(r) for r in rows],
    }


class HistoryPruneRequest(BaseModel):
    # Rows at least this many seconds old are deleted (float days-scale).
    retention_seconds: float | None = None
    # Keep only the newest N rows, deleting the rest.
    keep_last: int | None = None


class HistoryImportRequest(BaseModel):
    # Rows from a previous history export (stable ``id``s make import idempotent).
    rows: list[dict] = []
    # Overwrite rows whose id already exists instead of skipping them.
    replace: bool = False


@router.post("/tasks/{task_id}/prune-runs", summary="Prune a task's run history")
async def prune_runs(
    task_id: str,
    body: HistoryPruneRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not _scheduler.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    if body.retention_seconds is None and body.keep_last is None:
        raise HTTPException(
            status_code=422,
            detail="Provide 'retention_seconds' and/or 'keep_last'",
        )
    if body.retention_seconds is not None and body.retention_seconds < 0:
        raise HTTPException(status_code=422, detail="retention_seconds must be >= 0")
    if body.keep_last is not None and body.keep_last < 0:
        raise HTTPException(status_code=422, detail="keep_last must be >= 0")

    from backend.services.proactive_store import count_task_runs, prune_task_runs

    deleted = await prune_task_runs(
        session,
        task_id,
        retention_seconds=body.retention_seconds,
        keep_last=body.keep_last,
    )
    await session.commit()
    return {
        "task_id": task_id,
        "deleted": deleted,
        "remaining": await count_task_runs(session, task_id),
    }


@router.get("/history/export", summary="Export all task run history")
async def export_runs(session: AsyncSession = Depends(get_session)) -> dict:
    from datetime import datetime, timezone

    from backend.services.proactive_store import export_task_runs

    rows = await export_task_runs(session)
    return {
        "kind": "task_runs",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "rows": rows,
    }


@router.post("/history/import", summary="Restore task run history")
async def import_runs(
    body: HistoryImportRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.services.proactive_store import import_task_runs

    result = await import_task_runs(session, body.rows, replace=body.replace)
    await session.commit()
    return result


@router.post("/tasks/{task_id}/run", summary="Run a task now")
async def run_task(task_id: str) -> dict:
    task = await _scheduler.run_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task.to_dict()


@router.post("/tasks/{task_id}/pause", summary="Pause a task")
async def pause_task(
    task_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    task = _scheduler.pause_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    await save_scheduled_task(session, task)
    await session.commit()
    return task.to_dict()


@router.post("/tasks/{task_id}/resume", summary="Resume a task")
async def resume_task(
    task_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    task = _scheduler.resume_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    await save_scheduled_task(session, task)
    await session.commit()
    return task.to_dict()


@router.post("/tasks/{task_id}/cancel", summary="Cancel a task")
async def cancel_task(
    task_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    task = _scheduler.cancel_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    await save_scheduled_task(session, task)
    await session.commit()
    return task.to_dict()


@router.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task",
)
async def delete_task(
    task_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    if not _scheduler.delete_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    await delete_scheduled_task(session, task_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Notification endpoints ----

@router.post(
    "/notifications",
    status_code=status.HTTP_201_CREATED,
    summary="Create a notification",
)
async def create_notification(
    body: NotificationCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    level = _parse_member(_LEVEL_MAP, body.level, "level")
    channel = _parse_member(_CHANNEL_MAP, body.channel, "channel")
    record = await _notification_service.create(
        session,
        title=body.title,
        message=body.message,
        level=level.value,
        channel=channel.value,
        agent=body.agent,
        payload=body.payload,
    )
    await session.commit()
    await _notification_service.push(record)
    return record.to_dict()


@router.get("/notifications", summary="List notifications")
async def list_notifications(
    unread_only: bool = False,
    agent: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict:
    items = await _notification_service.list(
        session, unread_only=unread_only, agent=agent, limit=limit
    )
    unread = await _notification_service.unread_count(session)
    return {
        "notifications": [n.to_dict() for n in items],
        "unread_count": unread,
        "total": len(items),
    }


@router.get("/notifications/unread-count", summary="Get unread notification count")
async def unread_count(
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {"unread_count": await _notification_service.unread_count(session)}


@router.post(
    "/notifications/{notification_id}/read",
    summary="Mark notification as read",
)
async def mark_read(
    notification_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not await _notification_service.mark_read(session, notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    await session.commit()
    return {"success": True}


@router.post("/notifications/read-all", summary="Mark all as read")
async def mark_all_read(
    session: AsyncSession = Depends(get_session),
) -> dict:
    count = await _notification_service.mark_all_read(session)
    await session.commit()
    return {"marked_read": count}


@router.delete(
    "/notifications/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a notification",
)
async def delete_notification(
    notification_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not await _notification_service.delete(session, notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/notifications",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear all notifications",
)
async def clear_notifications(
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _notification_service.clear(session)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)