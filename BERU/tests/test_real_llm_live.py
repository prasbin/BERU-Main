"""Opt-in live smoke tests for the real OpenAI-compatible LLM provider.

The default suite is fully hermetic (mock provider, no network, no keys). These
tests execute a REAL HTTP request only when the operator explicitly opts in with
a working provider configuration through the environment — no key is ever
hardcoded:

    BERU_REAL_LLM_SMOKE=1    explicit opt-in gate
    LLM_BASE_URL=<endpoint>  required (e.g. https://api.openai.com/v1)
    LLM_MODEL=<model>        required
    LLM_API_KEY=<key>        optional (local endpoints often need none)

Without the gate the tests are skipped with an explanatory reason, so an
unconfigured environment reports *NOT AVAILABLE* (skipped) rather than a fake
pass. ``tests/conftest.py`` forces ``LLM_PROVIDER=mock`` for hermetic runs;
these tests deliberately ignore that and build the real provider directly from
the environment so the opt-in path is independent of the mock patching.

The tool-calling roundtrip test feeds the model an explicit *test-harness echo*
as the tool result (it cannot run a real BERU tool here); all it proves is that
the real endpoint accepts tool definitions, emits tool calls, and completes the
loop. BERU's own tool loop is covered deterministically by the simulated suites.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from backend.engines.llm.base import LLMMessage, ToolDefinition
from backend.engines.llm.openai_compatible import OpenAICompatibleProvider


def _live_config() -> dict[str, str] | None:
    """Return a provider config when the operator opted in, else ``None``."""
    if os.environ.get("BERU_REAL_LLM_SMOKE", "").strip() != "1":
        return None
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not base_url or not model:
        return None
    return {
        "base_url": base_url,
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "model": model,
    }


def _make_provider() -> OpenAICompatibleProvider:
    config = _live_config()
    if config is None:
        pytest.skip(
            "Real LLM smoke tests are opt-in: set BERU_REAL_LLM_SMOKE=1 plus "
            "LLM_BASE_URL and LLM_MODEL (see tests/test_real_llm_live.py)."
        )
    return OpenAICompatibleProvider(
        base_url=config["base_url"],
        api_key=config["api_key"],
        model=config["model"],
        timeout=120.0,
        max_retries=0,  # fail fast — a smoke test must never hang on retries
    )


async def test_live_chat_completion_returns_a_reply() -> None:
    """A real non-streaming completion returns content and a model id."""
    provider = _make_provider()
    try:
        response = await provider.chat(
            [LLMMessage(role="user", content="Reply with the single word: ok")]
        )
        assert response is not None
        assert response.content or response.finish_reason
        assert response.model
    finally:
        await provider.aclose()


async def test_live_streaming_emits_tokens() -> None:
    """A real streaming completion yields deltas and terminates cleanly."""
    provider = _make_provider()
    chunks: list[Any] = []
    try:
        async for chunk in provider.stream_chat(
            [LLMMessage(role="user", content="Count from 1 to 3.")]
        ):
            chunks.append(chunk)
            if chunk.done:
                break
        assert chunks
        assert any(c.done for c in chunks), "stream must terminate with a done chunk"
    finally:
        await provider.aclose()


async def test_live_tool_calling_roundtrip_when_supported() -> None:
    """Real endpoint round trip: tools are parsed, tool results are accepted.

    Skipped honestly when the live endpoint/model returns no tool call — that
    only registers the endpoint does not exercise function calling on this model.
    """
    provider = _make_provider()
    tools = [
        ToolDefinition(
            name="get_time",
            description="Get the current UTC time.",
            parameters={"type": "object", "properties": {}},
        )
    ]
    try:
        first = await provider.chat(
            [LLMMessage(role="user", content="Call get_time once, then stop.")],
            tools=tools,
        )
        if not first.tool_calls:
            pytest.skip(
                "live provider/model returned no tool call — function calling "
                "was not exercised on this endpoint/model"
            )
        call = first.tool_calls[0]
        assert call.name
        messages = [
            LLMMessage(role="user", content="Call get_time once, then stop."),
            LLMMessage(role="assistant", content="", tool_calls=[call]),
            # Explicit test-harness echo — not a fabricated BERU tool result.
            LLMMessage(
                role="tool",
                content='{"result": "test-harness echo of get_time"}',
                tool_call_id=call.id,
            ),
        ]
        final = await provider.chat(messages)
        assert final.content or final.finish_reason
    finally:
        await provider.aclose()