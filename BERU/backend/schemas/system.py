"""System-level schemas: health, status, and capability discovery."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    environment: str


class ComponentHealth(BaseModel):
    ok: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Result of the /ready probe: database ping + optional live LLM probe."""

    status: Literal["ok", "degraded", "error"]
    app: str
    version: str
    environment: str
    database: ComponentHealth
    llm: ComponentHealth
    llm_provider: str
    llm_probe_enabled: bool


class StatusResponse(BaseModel):
    """Richer runtime status for dashboards and diagnostics."""

    status: str
    app: str
    version: str
    environment: str
    llm_provider: str
    llm_model: str | None = None
    memory_window: int
    time: str
    reliability: dict | None = None


class ProactiveTaskMetric(BaseModel):
    name: str
    status: str
    run_count: int
    last_run: str | None = None


class ProactiveTriggerMetric(BaseModel):
    name: str
    fire_count: int
    last_fired: str | None = None


class ProactiveMetrics(BaseModel):
    scheduler_running: bool
    monitor_running: bool
    tasks: list[ProactiveTaskMetric] = []
    triggers: list[ProactiveTriggerMetric] = []


class RequestMetrics(BaseModel):
    uptime_s: float
    total_requests: int
    requests_per_1m: float
    by_status: dict[str, int] = {}
    by_error_type: dict[str, int] = {}


class MetricsResponse(BaseModel):
    """Aggregated process observability for ``/metrics``."""

    app: str
    version: str
    environment: str
    llm_provider: str
    requests: RequestMetrics
    active_sessions: int
    reliability: dict
    proactive: ProactiveMetrics


class AgentInfo(BaseModel):
    name: str
    description: str
    capabilities: list[str] = []


class ToolInfo(BaseModel):
    name: str
    description: str
    permissions: list[str] = []
    availability: Literal["available", "limited", "unavailable"] = "available"
    requires_confirmation: bool = False


class LLMTestResult(BaseModel):
    """Outcome of the most recent connection test against the LLM provider."""

    ok: bool
    connected: bool
    provider: str
    model: str
    latency_ms: float | None = None
    error: str | None = None
    note: str | None = None
    tested_at: str


class LLMInfo(BaseModel):
    """Current LLM configuration plus the most recent connection test."""

    provider: str
    model: str
    base_url: str
    has_api_key: bool
    configured: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float | None = None
    last_test: LLMTestResult | None = None
