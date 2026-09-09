"""Browser automation API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from backend.api.security import require_api_key
from backend.engines.browser import BrowserAction, PageStatus, get_browser_engine

router = APIRouter(
    prefix="/browser", tags=["browser"], dependencies=[Depends(require_api_key)]
)

_engine = get_browser_engine()


# ---- Request models ----


class PageCreateRequest(BaseModel):
    url: str = "about:blank"


class ActionRequest(BaseModel):
    action: str
    selector: str | None = None
    url: str | None = None
    text: str | None = None
    direction: str | None = None
    amount: int | None = None
    seconds: float | None = None
    key: str | None = None
    block: str | None = None
    value: str | None = None
    label: str | None = None
    index: int | None = None
    values: list[str] | None = None
    full_page: bool = False
    frame: str | None = None
    source: str | None = None
    target: str | None = None
    subcommand: str | None = None
    cookies: list[dict] | None = None
    glob: str | None = None
    paths: list[str] | None = None
    limit: int | None = None
    name: str | None = None
    redirect: str | None = None
    body: str | None = None
    content_type: str | None = None
    status_code: int | None = None
    headers: dict[str, str] | None = None
    headless: bool | None = None
    width: int | None = None
    height: int | None = None
    user_agent: str | None = None
    locale: str | None = None
    device_scale_factor: float | None = None
    is_mobile: bool | None = None
    has_touch: bool | None = None
    timezone_id: str | None = None


# ---- Availability ----


@router.get("/status", summary="Browser engine availability")
async def browser_status() -> dict:
    await _engine.ensure_ready()
    return _engine.status_info()


# ---- Page endpoints ----


@router.post(
    "/pages",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new browser page",
)
async def create_page(body: PageCreateRequest) -> dict:
    page = await _engine.create_page(body.url)
    return {"ok": page.status != PageStatus.ERROR, **page.to_dict()}


@router.get("/pages", summary="List all browser pages")
async def list_pages() -> dict:
    data = [p.to_dict() for p in _engine.list_pages()]
    return {"ok": True, "pages": data, "active_page": _engine._active_page_id}


@router.get("/pages/{page_id}", summary="Get a page")
async def get_page(page_id: str) -> dict:
    page = _engine.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return {"ok": True, **page.to_dict()}


@router.post("/pages/{page_id}/activate", summary="Set page as active")
async def activate_page(page_id: str) -> dict:
    if not _engine.set_active_page(page_id):
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return {"ok": True, "success": True, "active_page": page_id}


@router.delete(
    "/pages/{page_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Close a page",
)
async def close_page(page_id: str) -> Response:
    if not _engine.close_page(page_id):
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/close", summary="Close the entire browser session")
async def close_browser() -> dict:
    closed = await _engine.close_browser()
    return {"ok": True, "closed": closed, "pages": 0}


# ---- Action endpoints ----


@router.post("/action", summary="Execute a browser action")
async def execute_action(body: ActionRequest) -> dict:
    try:
        action = BrowserAction(body.action)
    except ValueError as err:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{body.action}'. Valid: {[a.value for a in BrowserAction]}",
        ) from err

    kwargs = {}
    if body.selector:
        kwargs["selector"] = body.selector
    if body.url:
        kwargs["url"] = body.url
    if body.text:
        kwargs["text"] = body.text
    if body.direction:
        kwargs["direction"] = body.direction
    if body.amount is not None:
        kwargs["amount"] = body.amount
    if body.seconds is not None:
        kwargs["seconds"] = body.seconds
    if body.key:
        kwargs["key"] = body.key
    if body.block:
        kwargs["block"] = body.block
    if body.value is not None:
        kwargs["value"] = body.value
    if body.label is not None:
        kwargs["label"] = body.label
    if body.index is not None:
        kwargs["index"] = body.index
    if body.values is not None:
        kwargs["values"] = list(body.values)
    if body.full_page:
        kwargs["full_page"] = body.full_page
    if body.frame:
        kwargs["frame"] = body.frame
    if body.source:
        kwargs["source"] = body.source
    if body.target:
        kwargs["target"] = body.target
    if body.subcommand:
        kwargs["subcommand"] = body.subcommand
    if body.cookies is not None:
        kwargs["cookies"] = list(body.cookies)
    if body.glob:
        kwargs["glob"] = body.glob
    if body.paths is not None:
        kwargs["paths"] = list(body.paths)
    if body.limit is not None:
        kwargs["limit"] = body.limit
    if body.name:
        kwargs["name"] = body.name
    if body.redirect:
        kwargs["redirect"] = body.redirect
    if body.body is not None:
        kwargs["body"] = body.body
    if body.content_type:
        kwargs["content_type"] = body.content_type
    if body.status_code is not None:
        kwargs["status_code"] = body.status_code
    if body.headers is not None:
        kwargs["headers"] = dict(body.headers)
    if body.headless is not None:
        kwargs["headless"] = body.headless
    if body.width is not None:
        kwargs["width"] = body.width
    if body.height is not None:
        kwargs["height"] = body.height
    if body.user_agent:
        kwargs["user_agent"] = body.user_agent
    if body.locale:
        kwargs["locale"] = body.locale
    if body.device_scale_factor is not None:
        kwargs["device_scale_factor"] = body.device_scale_factor
    if body.is_mobile is not None:
        kwargs["is_mobile"] = body.is_mobile
    if body.has_touch is not None:
        kwargs["has_touch"] = body.has_touch
    if body.timezone_id:
        kwargs["timezone_id"] = body.timezone_id

    result = await _engine.execute_action(action, **kwargs)
    return {"ok": result.success, **result.to_dict()}
