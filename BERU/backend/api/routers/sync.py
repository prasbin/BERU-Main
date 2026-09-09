"""Sync API: device registration, conversation deltas, phone-authored messages.

Provides the laptop <-> phone contract that the Android client consumes:
1. Register a device once (``POST /sync/devices``).
2. Pull new conversation/message deltas (``POST /sync/devices/{id}/pull``).
3. Push a message typed on the phone (``POST /sync/devices/{id}/messages``).
4. Receive relayed notifications via a per-device mailbox
   (``GET/POST .../mailbox``).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.security import require_api_key
from backend.core.errors import NotFoundError
from backend.database.base import get_session
from backend.services.sync_service import SyncService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/sync", tags=["sync"], dependencies=[Depends(require_api_key)]
)

_service = SyncService()


def get_sync_service() -> SyncService:
    """Return the process-wide sync service (engine + persistence wiring)."""
    return _service


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ServiceDep = Annotated[SyncService, Depends(get_sync_service)]


# ---- Request models ----


class RegisterDeviceRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Human-readable device name.")
    platform: str = "android"
    capabilities: list[str] = []
    push_url: str | None = None


class PullRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    # Devices may reuse the acked cursor explicitly; normally unset.
    cursor: int | None = None


class PushMessageRequest(BaseModel):
    conversation_id: str
    content: str = Field(..., min_length=1)


class NotifyRequest(BaseModel):
    title: str = Field(..., min_length=1)
    message: str = ""


class AckMailboxRequest(BaseModel):
    ids: list[str] = []


# ---- Device registry ----


@router.post(
    "/devices",
    status_code=status.HTTP_201_CREATED,
    response_model=None,
    summary="Register a remote device",
)
async def register_device(body: RegisterDeviceRequest, service: ServiceDep) -> dict:
    device = service.engine.register_device(
        name=body.name,
        platform=body.platform,
        capabilities=body.capabilities,
        push_url=body.push_url,
    )
    logger.info("Device registered: %s (%s)", device.device_id, body.name)
    return device.to_dict()


@router.get("/devices", summary="List registered devices")
async def list_devices(service: ServiceDep) -> list[dict]:
    return service.engine.list_devices()


@router.get("/devices/{device_id}", summary="Get a registered device")
async def get_device(device_id: str, service: ServiceDep) -> dict:
    device = service.engine.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Unknown sync device '{device_id}'.")
    return device.to_dict()


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a device",
)
async def revoke_device(device_id: str, service: ServiceDep) -> Response:
    if not service.engine.revoke_device(device_id):
        raise HTTPException(status_code=404, detail=f"Unknown sync device '{device_id}'.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Delta sync ----


@router.post("/devices/{device_id}/pull", summary="Pull conversation deltas")
async def pull(device_id: str, body: PullRequest, session: SessionDep, service: ServiceDep) -> dict:
    if body.cursor is not None:
        service.engine.set_cursor(device_id, body.cursor)
    try:
        return await service.pull(session, device_id, limit=body.limit)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/devices/{device_id}/messages",
    status_code=status.HTTP_201_CREATED,
    summary="Push a message authored on the device",
)
async def push_message(
    device_id: str,
    body: PushMessageRequest,
    session: SessionDep,
    service: ServiceDep,
) -> dict:
    try:
        return await service.push_message(
            session,
            device_id,
            conversation_id=body.conversation_id,
            content=body.content,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---- Push mailbox ----


@router.post(
    "/devices/{device_id}/notify",
    status_code=status.HTTP_201_CREATED,
    summary="Queue a relayed notification for a device",
)
async def notify(device_id: str, body: NotifyRequest, service: ServiceDep) -> dict:
    notification = service.engine.enqueue_notification(
        device_id, title=body.title, message=body.message
    )
    if notification is None:
        raise HTTPException(status_code=404, detail=f"Unknown sync device '{device_id}'.")
    return notification.to_dict()


@router.get("/devices/{device_id}/mailbox", summary="Read pending notifications")
async def mailbox(device_id: str, service: ServiceDep) -> list[dict]:
    service.engine.record_activity(device_id)
    return [n.to_dict() for n in service.engine.mailbox(device_id)]


@router.post(
    "/devices/{device_id}/mailbox/ack",
    summary="Acknowledge delivered notifications",
)
async def ack_mailbox(
    device_id: str, body: AckMailboxRequest, service: ServiceDep
) -> dict:
    return {"acknowledged": service.engine.ack_notifications(device_id, body.ids)}


# ---- Status ----


@router.get("/status", summary="Sync engine status")
async def sync_status(service: ServiceDep) -> dict:
    return service.engine.status()