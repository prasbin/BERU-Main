"""Reliability observability endpoints: activity ledger, approval audit trail."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.security import require_api_key
from backend.services.activity_ledger import get_activity_ledger

router = APIRouter(
    prefix="/reliability",
    tags=["reliability"],
    dependencies=[Depends(require_api_key)],
)


def _ledger():
    # Resolve at request time so test resets of the singleton take effect.
    return get_activity_ledger()


@router.get("/activity", summary="Recent tool-call activity")
async def activity(limit: int = 50) -> dict:
    return {"ok": True, "entries": _ledger().list_activity(limit=limit)}


@router.get("/audit", summary="Approval/denial audit trail")
async def audit(limit: int = 50) -> dict:
    return {"ok": True, "entries": _ledger().list_audit(limit=limit)}


@router.get("/summary", summary="Aggregated reliability summary")
async def summary() -> dict:
    return {"ok": True, "data": _ledger().summary()}


@router.delete("/activity", summary="Clear the ledger (in-memory + persisted rows)")
async def clear() -> dict:
    ledger = _ledger()
    ledger.clear()
    purged = await ledger.purge()
    return {"ok": True, "cleared": True, "purged": purged}
