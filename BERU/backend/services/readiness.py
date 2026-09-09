"""Readiness probing: database ping + optional live LLM probe.

Used by the ``/ready`` endpoint so orchestrators can distinguish "process is
up" (``/health``) from "process can actually serve requests" (DB reachable and,
when enabled, the LLM answers). The built-in mock provider is always probed
(offline and instant); live providers are probed only when
``settings.health_llm_probe`` is true so a paid API isn't billed on every
scheduled healthcheck tick.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReadinessResult:
    """Outcome of one readiness evaluation."""

    database_ok: bool
    database_detail: str | None = None
    llm_ok: bool = True
    llm_detail: str | None = None
    llm_provider: str = ""
    llm_probed: bool = False

    @property
    def status(self) -> str:
        if not self.database_ok:
            return "error"
        if not self.llm_ok:
            return "degraded"
        return "ok"


async def check_readiness(
    engine: Any,
    provider: Any,
    *,
    llm_probe: bool,
    timeout_s: float = 60.0,
) -> ReadinessResult:
    """Evaluate database and (optionally) LLM readiness.

    ``engine`` is any SQLAlchemy async engine; ``provider`` is the active
    ``LLMProvider``. The database is always checked with a ``SELECT 1``; the
    LLM is probed when ``llm_probe`` is true or the provider is the offline
    mock (which costs nothing).
    """
    database_ok = True
    database_detail: str | None = None
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - report any connectivity failure
        database_ok = False
        database_detail = f"{type(exc).__name__}: {exc}"

    llm_ok = True
    llm_detail: str | None = None
    llm_probed = llm_probe or provider.name == "mock"
    if llm_probed:
        started = time.perf_counter()
        try:
            await asyncio.wait_for(provider.probe(), timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - report any probe failure
            llm_ok = False
            llm_detail = f"{type(exc).__name__}: {exc}"
        else:
            llm_detail = f"probe ok in {(time.perf_counter() - started) * 1000:.0f} ms"
    else:
        llm_detail = (
            "LLM probe disabled (set BERU_HEALTH_LLM_PROBE=true to enable live probing)"
        )

    return ReadinessResult(
        database_ok=database_ok,
        database_detail=database_detail,
        llm_ok=llm_ok,
        llm_detail=llm_detail,
        llm_provider=provider.name,
        llm_probed=llm_probed,
    )