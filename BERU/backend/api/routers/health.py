"""Health and readiness endpoints.

``/health`` is a lightweight liveness probe (process is up). ``/ready`` goes
further and exercises the database (``SELECT 1``) and — when
``BERU_HEALTH_LLM_PROBE=true`` or the offline mock provider is active — the LLM,
so load balancers / orchestrators can stop routing before BERU can actually
serve a request.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from backend.core.config import Settings, get_settings
from backend.database.base import get_engine
from backend.engines.llm.registry import get_llm_provider
from backend.schemas.system import ComponentHealth, HealthResponse, ReadinessResponse
from backend.services.readiness import check_readiness

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Lightweight liveness probe — confirms the process is up and serving."""
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.version,
        environment=settings.environment,
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness check (DB + LLM probe)",
    responses={503: {"description": "Service not ready (DB unreachable or LLM probe failed)"}},
)
async def ready(
    response: Response,
    settings: Settings = Depends(get_settings),
    engine=Depends(get_engine),
    provider=Depends(get_llm_provider),
) -> ReadinessResponse:
    """Prove the service can actually serve: DB reachable and LLM answering.

    Returns 200 when ready, otherwise 503 (with the failure detail in the
    body) so orchestrators can take BERU out of rotation.
    """
    result = await check_readiness(
        engine,
        provider,
        llm_probe=settings.health_llm_probe,
        timeout_s=settings.llm_timeout,
    )
    response.status_code = 200 if result.status == "ok" else 503
    return ReadinessResponse(
        status=result.status,
        app=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        database=ComponentHealth(ok=result.database_ok, detail=result.database_detail),
        llm=ComponentHealth(ok=result.llm_ok, detail=result.llm_detail),
        llm_provider=result.llm_provider,
        llm_probe_enabled=result.llm_probed,
    )