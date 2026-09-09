"""Event monitoring API — data sources, triggers, and manual ticks.

Uses the shared proactive runtime monitor so triggers created here fire against
the same state the background loop and the rest of the app observe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.security import require_api_key
from backend.database.base import get_session
from backend.engines.monitor import (
    ConditionType,
    Trigger,
    TriggerCondition,
    TriggerStatus,
)
from backend.services.proactive_service import get_event_monitor
from backend.services.proactive_store import (
    delete_monitor_trigger,
    save_monitor_trigger,
)

router = APIRouter(prefix="/monitor", tags=["monitor"], dependencies=[Depends(require_api_key)])

_CONDITION_TYPES = {c.value: c for c in ConditionType}
_TRIGGER_STATUSES = {s.value: s for s in TriggerStatus}

_monitor = get_event_monitor()


# ---- Request models ----


class TriggerCreateRequest(BaseModel):
    name: str
    description: str = ""
    condition_type: str = "threshold"
    source: str = ""
    operator: str = "eq"
    value: Any = None
    params: dict = {}
    actions: list[dict] = []
    cooldown_seconds: float = 0
    status: str = "active"
    retention_days: float | None = None
    keep_last: int | None = None


class TriggerUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    cooldown_seconds: float | None = None
    retention_days: float | None = None
    keep_last: int | None = None


class SourceValueRequest(BaseModel):
    value: Any


def _parse_enum(mapping: dict, value: str, label: str):
    try:
        return mapping[value]
    except KeyError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown {label} '{value}'. Valid values: {sorted(mapping)}",
        ) from None


# ---- Status / sources ----


@router.get("/status", summary="Monitor runtime status")
async def monitor_status() -> dict:
    return {
        "running": _monitor.running,
        "sources": sorted(_monitor.list_sources()),
        "triggers": len(_monitor.list_triggers()),
    }


@router.get("/sources", summary="List registered data sources")
async def list_sources() -> dict:
    return {"sources": sorted(_monitor.list_sources())}


@router.post("/sources/{source}", summary="Push a current value for a source")
async def update_source_value(source: str, body: SourceValueRequest) -> dict:
    _monitor.update_source_value(source, body.value)
    return {"source": source, "value": body.value}


# ---- Triggers ----


@router.post(
    "/triggers",
    status_code=status.HTTP_201_CREATED,
    summary="Create a monitoring trigger",
)
async def create_trigger(
    body: TriggerCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    condition_type = _parse_enum(_CONDITION_TYPES, body.condition_type, "condition_type")
    trigger_status = _parse_enum(_TRIGGER_STATUSES, body.status, "status")
    if body.source is None:
        raise HTTPException(status_code=422, detail="A trigger needs a 'source'")
    trigger = Trigger(
        name=body.name,
        description=body.description,
        status=trigger_status,
        condition=TriggerCondition(
            condition_type=condition_type,
            source=body.source,
            operator=body.operator,
            value=body.value,
            params=body.params,
        ),
        actions=body.actions,
        cooldown_seconds=max(0.0, body.cooldown_seconds),
        retention_days=body.retention_days,
        keep_last=body.keep_last,
    )
    _monitor.add_trigger(trigger)
    # Persist so the trigger survives restarts (and restore keeps it in sync).
    await save_monitor_trigger(session, trigger)
    await session.commit()
    return trigger.to_dict()


@router.get("/triggers", summary="List all triggers")
async def list_triggers() -> list[dict]:
    return [t.to_dict() for t in _monitor.list_triggers()]


@router.get("/triggers/{trigger_id}", summary="Get a trigger")
async def get_trigger(trigger_id: str) -> dict:
    trigger = _monitor.get_trigger(trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    return trigger.to_dict()


@router.patch("/triggers/{trigger_id}", summary="Update a monitoring trigger")
async def update_trigger(
    trigger_id: str,
    body: TriggerUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    trigger = _monitor.get_trigger(trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    if body.name is not None:
        trigger.name = body.name
    if body.description is not None:
        trigger.description = body.description
    if body.cooldown_seconds is not None:
        trigger.cooldown_seconds = max(0.0, body.cooldown_seconds)
    if body.retention_days is not None or "retention_days" in body.model_fields_set:
        trigger.retention_days = body.retention_days
    if body.keep_last is not None or "keep_last" in body.model_fields_set:
        trigger.keep_last = body.keep_last
    await save_monitor_trigger(session, trigger)
    await session.commit()
    return trigger.to_dict()


@router.get("/triggers/{trigger_id}/fires", summary="Get a trigger's fire history")
async def trigger_fires(
    trigger_id: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not _monitor.get_trigger(trigger_id):
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    from backend.services.proactive_store import (
        count_trigger_fires,
        list_trigger_fires,
        trigger_fire_summary,
        trigger_fire_to_dict,
    )

    rows = await list_trigger_fires(session, trigger_id, limit=min(max(limit, 1), 500))
    return {
        "trigger_id": trigger_id,
        "total": await count_trigger_fires(session, trigger_id),
        "summary": await trigger_fire_summary(session, trigger_id),
        "fires": [trigger_fire_to_dict(r) for r in rows],
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


@router.post("/triggers/{trigger_id}/prune-fires", summary="Prune a trigger's fire history")
async def prune_fires(
    trigger_id: str,
    body: HistoryPruneRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not _monitor.get_trigger(trigger_id):
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    if body.retention_seconds is None and body.keep_last is None:
        raise HTTPException(
            status_code=422,
            detail="Provide 'retention_seconds' and/or 'keep_last'",
        )
    if body.retention_seconds is not None and body.retention_seconds < 0:
        raise HTTPException(status_code=422, detail="retention_seconds must be >= 0")
    if body.keep_last is not None and body.keep_last < 0:
        raise HTTPException(status_code=422, detail="keep_last must be >= 0")

    from backend.services.proactive_store import (
        count_trigger_fires,
        prune_trigger_fires,
    )

    deleted = await prune_trigger_fires(
        session,
        trigger_id,
        retention_seconds=body.retention_seconds,
        keep_last=body.keep_last,
    )
    await session.commit()
    return {
        "trigger_id": trigger_id,
        "deleted": deleted,
        "remaining": await count_trigger_fires(session, trigger_id),
    }


@router.get("/history/export", summary="Export all trigger fire history")
async def export_fires(session: AsyncSession = Depends(get_session)) -> dict:
    from datetime import datetime, timezone

    from backend.services.proactive_store import export_trigger_fires

    rows = await export_trigger_fires(session)
    return {
        "kind": "trigger_fires",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "rows": rows,
    }


@router.post("/history/import", summary="Restore trigger fire history")
async def import_fires(
    body: HistoryImportRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.services.proactive_store import import_trigger_fires

    result = await import_trigger_fires(session, body.rows, replace=body.replace)
    await session.commit()
    return result


@router.post("/triggers/{trigger_id}/pause", summary="Pause a trigger")
async def pause_trigger(
    trigger_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    trigger = _monitor.pause_trigger(trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    await save_monitor_trigger(session, trigger)
    await session.commit()
    return trigger.to_dict()


@router.post("/triggers/{trigger_id}/resume", summary="Resume a trigger")
async def resume_trigger(
    trigger_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    trigger = _monitor.resume_trigger(trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    await save_monitor_trigger(session, trigger)
    await session.commit()
    return trigger.to_dict()


@router.post("/triggers/{trigger_id}/disable", summary="Disable a trigger")
async def disable_trigger(
    trigger_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    trigger = _monitor.disable_trigger(trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    await save_monitor_trigger(session, trigger)
    await session.commit()
    return trigger.to_dict()


@router.delete(
    "/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a trigger",
)
async def delete_trigger(
    trigger_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    if not _monitor.delete_trigger(trigger_id):
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")
    await delete_monitor_trigger(session, trigger_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Execution control ----


@router.post("/tick", summary="Refresh sources and evaluate triggers once")
async def tick() -> dict:
    from datetime import datetime, timezone

    fired = await _monitor.check_once()
    return {"fired": fired, "checked_at": datetime.now(timezone.utc).isoformat()}


@router.post("/start", summary="Start the background monitor loop")
async def start_monitor() -> dict:
    await _monitor.start()
    return {"running": _monitor.running}


@router.post("/stop", summary="Stop the background monitor loop")
async def stop_monitor() -> dict:
    await _monitor.stop()
    return {"running": False}