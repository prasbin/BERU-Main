"""System control API endpoints: commands, app launching, notifications."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from backend.api.security import require_api_key
from backend.engines.app_launcher import get_app_launcher
from backend.engines.command import get_command_executor
from backend.engines.notifier import get_os_notifier

router = APIRouter(
    prefix="/system-control",
    tags=["system-control"],
    dependencies=[Depends(require_api_key)],
)

# Shared process-wide engines: the agent tools and these endpoints observe the
# same execution history (commands, launches, notifications).
_executor = get_command_executor()
_launcher = get_app_launcher()
_notifier = get_os_notifier()


# ---- Request models ----


class CommandRequest(BaseModel):
    command: str
    timeout: float | None = None
    cwd: str | None = None


class LaunchRequest(BaseModel):
    app_name: str
    args: list[str] = []
    cwd: str | None = None


class NotifyRequest(BaseModel):
    title: str
    message: str
    app_name: str = "BERU"


# ---- Command endpoints ----


@router.post("/commands", summary="Execute a system command")
async def execute_command(body: CommandRequest) -> dict:
    result = await _executor.execute(
        body.command, timeout=body.timeout, cwd=body.cwd
    )
    if result.status.value == "blocked":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Command blocked",
                "reason": result.blocked_reason,
            },
        )
    return result.to_dict()


@router.get("/commands", summary="List command execution history")
async def command_history(limit: int = 50) -> list[dict]:
    return [r.to_dict() for r in _executor.get_history(limit)]


@router.delete(
    "/commands",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear command history",
)
async def clear_command_history() -> Response:
    _executor.clear_history()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- App launching endpoints ----


@router.post(
    "/apps/launch",
    status_code=status.HTTP_201_CREATED,
    summary="Launch an application",
)
async def launch_app(body: LaunchRequest) -> dict:
    result = await _launcher.launch(
        body.app_name, args=body.args, cwd=body.cwd
    )
    return result.to_dict()


@router.get("/apps", summary="List launched applications")
async def list_launched_apps() -> list[dict]:
    return [r.to_dict() for r in _launcher.list_launched()]


@router.get("/apps/aliases", summary="List available app aliases")
async def list_app_aliases() -> dict:
    return _launcher._aliases


# ---- Notification endpoints ----


@router.post(
    "/notifications",
    status_code=status.HTTP_201_CREATED,
    summary="Send a desktop notification",
)
async def send_notification(body: NotifyRequest) -> dict:
    result = await _notifier.send(
        title=body.title, message=body.message, app_name=body.app_name
    )
    return result.to_dict()


@router.get("/notifications", summary="List notification history")
async def notification_history(limit: int = 50) -> list[dict]:
    return [n.to_dict() for n in _notifier.get_history(limit)]


@router.get("/notifications/status", summary="Check OS notification availability")
async def notification_status() -> dict:
    return {"available": _notifier.is_available}