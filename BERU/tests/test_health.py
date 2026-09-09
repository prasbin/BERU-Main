"""Health and readiness endpoint tests.

Covers /health (liveness) and /ready (readiness: DB ping + LLM probe),
including the probe-failure paths of the readiness service.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.services.readiness import check_readiness


class _FakeProvider:
    """Minimal provider duck for service-level tests."""

    name = "fake"

    def __init__(self, *, fails: bool = False) -> None:
        self._fails = fails
        self.calls = 0

    async def probe(self) -> None:
        self.calls += 1
        if self._fails:
            raise RuntimeError("boom")


async def test_health_liveness(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["app"]
    assert data["version"]


async def test_ready_ok_with_mock_provider(client):
    """With the offline mock provider the LLM is always probed; DB is pinged."""
    resp = await client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"]["ok"] is True
    assert data["database"]["detail"] is None
    assert data["llm"]["ok"] is True
    assert "probe ok" in (data["llm"]["detail"] or "")
    assert data["llm_provider"] == "mock"
    assert data["llm_probe_enabled"] is True


async def test_ready_database_failure_returns_error(_engine, tmp_path):
    provider = _FakeProvider()
    result = await check_readiness(_engine, provider, llm_probe=False)
    assert result.status == "ok"

    # An engine pointing into a missing directory fails to connect.
    broken = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'no_such_dir' / 'beru.db').as_posix()}"
    )
    result = await check_readiness(broken, provider, llm_probe=False)
    assert result.status == "error"
    assert result.database_ok is False
    assert result.database_detail
    await broken.dispose()


async def test_ready_llm_probe_disabled_for_live_provider_is_ok(_engine):
    """A non-mock provider is not probed unless BERU_HEALTH_LLM_PROBE is set."""
    provider = _FakeProvider()
    result = await check_readiness(_engine, provider, llm_probe=False)
    assert result.status == "ok"
    assert result.llm_ok is True
    assert result.llm_probed is False
    assert provider.calls == 0
    assert "BERU_HEALTH_LLM_PROBE" in (result.llm_detail or "")


async def test_ready_llm_probe_failure_marks_degraded(_engine):
    provider = _FakeProvider(fails=True)
    result = await check_readiness(_engine, provider, llm_probe=True)
    assert result.status == "degraded"
    assert result.database_ok is True
    assert result.llm_ok is False
    assert result.llm_probed is True
    assert "boom" in (result.llm_detail or "")
    assert provider.calls == 1


@pytest.mark.parametrize("fails", [False, True])
async def test_mock_provider_is_always_probed(_engine, fails):
    """The offline mock provider is probed regardless of the env flag."""
    provider = _FakeProvider()
    provider.name = "mock"
    if fails:
        provider._fails = True
    result = await check_readiness(_engine, provider, llm_probe=False)
    assert result.llm_probed is True
    assert provider.calls == 1
    if fails:
        assert result.status == "degraded"
        assert result.llm_ok is False
    else:
        assert result.status == "ok"
        assert result.llm_ok is True