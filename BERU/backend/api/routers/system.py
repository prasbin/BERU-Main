"""System endpoints: runtime status, capability discovery, LLM health.

``GET /api/v1/tools`` reports the real tool surface exposed by every registered
agent (deduplicated by name) plus an honest ``availability`` rating for each —
so panels can show 🟢/🟡/🔴 instead of pretending stubs work.

``GET`` / ``POST /api/v1/llm`` describe the current LLM configuration and let
the UI run a live connection test. ``connected`` is only ever reported after an
actual successful test call — never inferred from configuration alone.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from backend.agents.registry import AgentRegistry, get_agent_registry
from backend.api.security import require_api_key, session_count
from backend.core.config import Settings, get_settings
from backend.engines.llm.base import LLMMessage, LLMProvider
from backend.engines.llm.registry import get_llm_provider
from backend.schemas.system import (
    AgentInfo,
    LLMInfo,
    LLMTestResult,
    MetricsResponse,
    ProactiveMetrics,
    ProactiveTaskMetric,
    ProactiveTriggerMetric,
    RequestMetrics,
    StatusResponse,
    ToolInfo,
)
from backend.services.activity_ledger import get_activity_ledger
from backend.services.observability import get_request_tracker

router = APIRouter(tags=["system"], dependencies=[Depends(require_api_key)])

# Process-wide cache of the last LLM connection test (shared across requests).
_last_llm_test: LLMTestResult | None = None
_llm_test_lock = asyncio.Lock()


@router.get("/status", response_model=StatusResponse, summary="Runtime status")
async def status(settings: Settings = Depends(get_settings)) -> StatusResponse:
    provider = get_llm_provider()
    return StatusResponse(
        status="ok",
        app=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        llm_provider=provider.name,
        llm_model=settings.llm_model,
        memory_window=settings.memory_window_size,
        time=datetime.now(timezone.utc).isoformat(),
        reliability=get_activity_ledger().summary(),
    )


def _proactive_metrics() -> ProactiveMetrics:
    """Aggregate scheduler/monitor run rates without starting background loops."""
    tasks: list[ProactiveTaskMetric] = []
    triggers: list[ProactiveTriggerMetric] = []
    scheduler_running = monitor_running = False
    try:
        from backend.services.proactive_service import get_event_monitor, get_scheduler

        scheduler = get_scheduler()
        monitor = get_event_monitor()
        scheduler_running = scheduler.running
        monitor_running = monitor.running
        for t in scheduler.list_tasks():
            tasks.append(
                ProactiveTaskMetric(
                    name=t.name or t.id,
                    status=t.status.value,
                    run_count=t.run_count,
                    last_run=t.last_run.isoformat() if t.last_run else None,
                )
            )
        for trg in monitor.list_triggers():
            triggers.append(
                ProactiveTriggerMetric(
                    name=trg.name or trg.id,
                    fire_count=trg.fire_count,
                    last_fired=trg.last_fired.isoformat() if trg.last_fired else None,
                )
            )
    except Exception:  # noqa: BLE001 - metrics must never fail the request
        return ProactiveMetrics(scheduler_running=False, monitor_running=False)
    return ProactiveMetrics(
        scheduler_running=scheduler_running,
        monitor_running=monitor_running,
        tasks=tasks,
        triggers=triggers,
    )


@router.get("/metrics", response_model=MetricsResponse, summary="Process observability metrics")
async def metrics(
    settings: Settings = Depends(get_settings),
) -> MetricsResponse:
    """Aggregate request, error, ledger, sessions, and proactive run metrics.

    All values are process-local (reset on restart). ``rate`` is measured over a
    trailing 60-second window.
    """
    snapshot = get_request_tracker().snapshot()
    return MetricsResponse(
        app=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        llm_provider=get_llm_provider().name,
        requests=RequestMetrics(**snapshot),
        active_sessions=session_count(),
        reliability=get_activity_ledger().summary(),
        proactive=_proactive_metrics(),
    )


@router.get("/agents", response_model=list[AgentInfo], summary="List available agents")
async def list_agents(
    registry: AgentRegistry = Depends(get_agent_registry),
) -> list[AgentInfo]:
    return [
        AgentInfo(name=a.name, description=a.description, capabilities=list(a.capabilities))
        for a in registry.list()
    ]


def _all_tools(registry: AgentRegistry) -> list[Any]:
    """Collect every tool exposed by every registered agent (unique by name)."""
    by_name: dict[str, Any] = {}
    for agent in registry.list():
        for tool in agent.list_tools():
            by_name.setdefault(tool.name, tool)
    return sorted(by_name.values(), key=lambda t: t.name)


@router.get("/tools", response_model=list[ToolInfo], summary="List available tools")
async def list_tools(
    registry: AgentRegistry = Depends(get_agent_registry),
) -> list[ToolInfo]:
    return [
        ToolInfo(
            name=t.name,
            description=t.description,
            permissions=list(t.permissions),
            availability=t.availability,
            requires_confirmation=t.requires_confirmation,
        )
        for t in _all_tools(registry)
    ]


def _provider_connected(provider: LLMProvider) -> bool:
    """A provider counts as connected for the UI when it is not the mock."""
    return provider.name != "mock"


@router.get("/llm", response_model=LLMInfo, summary="Current LLM configuration")
async def llm_info(settings: Settings = Depends(get_settings)) -> LLMInfo:
    return LLMInfo(
        provider=settings.llm_provider,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        has_api_key=bool(settings.llm_api_key),
        configured=_provider_connected(get_llm_provider()),
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
        last_test=_last_llm_test,
    )


async def _run_llm_test(settings: Settings) -> LLMTestResult:
    """Perform one minimal generation against the configured provider.

    A mock provider "succeeds" instantly but is reported as ``not connected``
    so the UI never claims a real model is reachable when it isn't.
    """
    provider = get_llm_provider()
    started = time.perf_counter()
    note = None
    try:
        await asyncio.wait_for(
            provider.chat(
                [LLMMessage(role="user", content="Reply with the single word 'ok'.")],
                model=settings.llm_model,
                temperature=0.0,
                max_tokens=4,
            ),
            timeout=settings.llm_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - report any provider failure to the UI
        return LLMTestResult(
            ok=False,
            connected=False,
            provider=provider.name,
            model=settings.llm_model,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=str(exc),
            tested_at=datetime.now(timezone.utc).isoformat(),
        )
    if not _provider_connected(provider):
        note = (
            "Configuration defaulted to the built-in mock provider (no real "
            "model). Set LLM_PROVIDER/openai_compatible details to connect."
        )
    return LLMTestResult(
        ok=True,
        connected=_provider_connected(provider),
        provider=provider.name,
        model=settings.llm_model,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        note=note,
        tested_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/llm/test",
    response_model=LLMTestResult,
    summary="Run a live LLM connection test",
)
async def llm_test(settings: Settings = Depends(get_settings)) -> LLMTestResult:
    global _last_llm_test
    async with _llm_test_lock:
        result = await _run_llm_test(settings)
        _last_llm_test = result
        return result