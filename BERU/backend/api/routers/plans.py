"""Plan management endpoints: create, list, execute, and manage plans."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from backend.api.security import require_api_key
from backend.services.plan_service import PlanService

router = APIRouter(
    prefix="/plans", tags=["planning"], dependencies=[Depends(require_api_key)]
)

_plan_service = PlanService()


# ---- Request/Response models ----


class PlanCreateRequest(BaseModel):
    goal: str


class ManualPlanCreateRequest(BaseModel):
    goal: str
    steps: list[dict]


class StepExecuteRequest(BaseModel):
    step_id: str


class StepCompleteRequest(BaseModel):
    result: str = "Done."


class StepFailRequest(BaseModel):
    error: str


class PlanRead(BaseModel):
    id: str
    goal: str
    steps: list[dict]
    status: str
    progress_pct: float


class StepRead(BaseModel):
    id: str
    description: str
    agent: str | None
    tools: list[str]
    dependencies: list[str]
    status: str
    result: str | None
    error: str | None


# ---- Endpoints ----


@router.post(
    "",
    response_model=PlanRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a plan by decomposing a goal",
)
async def create_plan(body: PlanCreateRequest) -> PlanRead:
    """Decompose a goal into a multi-step plan using the LLM."""
    plan = await _plan_service.create_plan(body.goal)
    return PlanRead(
        id=plan.id,
        goal=plan.goal,
        steps=[s.to_dict() for s in plan.steps],
        status=plan.status.value,
        progress_pct=plan.progress_pct,
    )


@router.post(
    "/manual",
    response_model=PlanRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a plan manually from steps",
)
async def create_manual_plan(body: ManualPlanCreateRequest) -> PlanRead:
    """Create a plan with manually specified steps."""
    plan = _plan_service.create_manual_plan(body.goal, body.steps)
    return PlanRead(
        id=plan.id,
        goal=plan.goal,
        steps=[s.to_dict() for s in plan.steps],
        status=plan.status.value,
        progress_pct=plan.progress_pct,
    )


@router.get("", response_model=list[PlanRead], summary="List all plans")
async def list_plans() -> list[PlanRead]:
    """List all plans."""
    plans = _plan_service.list_plans()
    return [
        PlanRead(
            id=p.id,
            goal=p.goal,
            steps=[s.to_dict() for s in p.steps],
            status=p.status.value,
            progress_pct=p.progress_pct,
        )
        for p in plans
    ]


@router.get("/active", response_model=list[PlanRead], summary="List active plans")
async def list_active_plans() -> list[PlanRead]:
    """List plans that are in progress or draft."""
    plans = _plan_service.list_active_plans()
    return [
        PlanRead(
            id=p.id,
            goal=p.goal,
            steps=[s.to_dict() for s in p.steps],
            status=p.status.value,
            progress_pct=p.progress_pct,
        )
        for p in plans
    ]


@router.get("/{plan_id}", response_model=PlanRead, summary="Get a plan")
async def get_plan(plan_id: str) -> PlanRead:
    """Get a plan by ID."""
    plan = _plan_service.get_plan(plan_id)
    return PlanRead(
        id=plan.id,
        goal=plan.goal,
        steps=[s.to_dict() for s in plan.steps],
        status=plan.status.value,
        progress_pct=plan.progress_pct,
    )


@router.post(
    "/{plan_id}/steps/{step_id}/start",
    response_model=dict,
    summary="Start a step",
)
async def start_step(plan_id: str, step_id: str) -> dict:
    """Start execution of a specific step."""
    result = _plan_service.start_step(plan_id, step_id)
    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=result["error"]
        )
    return result


@router.post(
    "/{plan_id}/steps/{step_id}/complete",
    response_model=PlanRead,
    summary="Complete a step",
)
async def complete_step(
    plan_id: str, step_id: str, body: StepCompleteRequest | None = None
) -> PlanRead:
    """Mark a step as completed."""
    result_text = body.result if body else "Done."
    plan = _plan_service.complete_step(plan_id, step_id, result_text)
    return PlanRead(
        id=plan.id,
        goal=plan.goal,
        steps=[s.to_dict() for s in plan.steps],
        status=plan.status.value,
        progress_pct=plan.progress_pct,
    )


@router.post(
    "/{plan_id}/steps/{step_id}/fail",
    response_model=PlanRead,
    summary="Fail a step",
)
async def fail_step(plan_id: str, step_id: str, body: StepFailRequest) -> PlanRead:
    """Mark a step as failed."""
    plan = _plan_service.fail_step(plan_id, step_id, body.error)
    return PlanRead(
        id=plan.id,
        goal=plan.goal,
        steps=[s.to_dict() for s in plan.steps],
        status=plan.status.value,
        progress_pct=plan.progress_pct,
    )


@router.post(
    "/{plan_id}/steps/{step_id}/skip",
    response_model=PlanRead,
    summary="Skip a step",
)
async def skip_step(plan_id: str, step_id: str) -> PlanRead:
    """Skip a step."""
    plan = _plan_service.skip_step(plan_id, step_id)
    return PlanRead(
        id=plan.id,
        goal=plan.goal,
        steps=[s.to_dict() for s in plan.steps],
        status=plan.status.value,
        progress_pct=plan.progress_pct,
    )


@router.post(
    "/{plan_id}/cancel",
    response_model=PlanRead,
    summary="Cancel a plan",
)
async def cancel_plan(plan_id: str) -> PlanRead:
    """Cancel a plan, skipping all pending steps."""
    plan = _plan_service.cancel_plan(plan_id)
    return PlanRead(
        id=plan.id,
        goal=plan.goal,
        steps=[s.to_dict() for s in plan.steps],
        status=plan.status.value,
        progress_pct=plan.progress_pct,
    )


@router.delete(
    "/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a plan",
)
async def delete_plan(plan_id: str) -> Response:
    """Delete a plan."""
    _plan_service.delete_plan(plan_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
