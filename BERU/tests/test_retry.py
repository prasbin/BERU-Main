"""Tests for retry/backoff and timeout hardening in the OpenAI-compatible provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.core.errors import LLMProviderError
from backend.engines.llm.base import LLMMessage
from backend.engines.llm.openai_compatible import OpenAICompatibleProvider


def _make_provider(**kwargs) -> OpenAICompatibleProvider:
    defaults = {
        "base_url": "http://localhost:8080/v1",
        "api_key": "test-key",
        "model": "test-model",
        "max_retries": 3,
        "retry_base_delay": 0.01,  # fast for tests
        "retry_max_delay": 0.1,
    }
    defaults.update(kwargs)
    return OpenAICompatibleProvider(**defaults)


def _ok_response():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "model": "test-model",
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    return resp


def _retryable_response(status_code: int, headers: dict | None = None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = {"error": {"message": "rate limited"}}
    return resp


# ---- Retry tests ----


@pytest.mark.asyncio
async def test_no_retry_on_success():
    """Successful response should not trigger any retries."""
    provider = _make_provider()
    mock_post = AsyncMock(return_value=_ok_response())

    with patch.object(provider._get_client(), "post", mock_post):
        resp = await provider.chat([LLMMessage(role="user", content="hi")])

    assert resp.content == "ok"
    assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_retries_on_429():
    """429 should trigger retries, then succeed."""
    provider = _make_provider()
    mock_post = AsyncMock(
        side_effect=[
            _retryable_response(429),
            _retryable_response(429),
            _ok_response(),
        ]
    )

    with patch.object(provider._get_client(), "post", mock_post):
        resp = await provider.chat([LLMMessage(role="user", content="hi")])

    assert resp.content == "ok"
    assert mock_post.call_count == 3


@pytest.mark.asyncio
async def test_retries_on_500():
    """500 should trigger retries."""
    provider = _make_provider()
    mock_post = AsyncMock(side_effect=[_retryable_response(500), _ok_response()])

    with patch.object(provider._get_client(), "post", mock_post):
        resp = await provider.chat([LLMMessage(role="user", content="hi")])

    assert resp.content == "ok"
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_retries_on_502():
    """502 should trigger retries."""
    provider = _make_provider()
    mock_post = AsyncMock(side_effect=[_retryable_response(502), _ok_response()])

    with patch.object(provider._get_client(), "post", mock_post):
        resp = await provider.chat([LLMMessage(role="user", content="hi")])

    assert resp.content == "ok"
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_retries_on_503():
    """503 should trigger retries."""
    provider = _make_provider()
    mock_post = AsyncMock(side_effect=[_retryable_response(503), _ok_response()])

    with patch.object(provider._get_client(), "post", mock_post):
        resp = await provider.chat([LLMMessage(role="user", content="hi")])

    assert resp.content == "ok"
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_retries_on_network_error():
    """Network errors (TransportError) should trigger retries."""
    provider = _make_provider()
    mock_post = AsyncMock(
        side_effect=[
            httpx.ConnectError("Connection refused"),
            _ok_response(),
        ]
    )

    with patch.object(provider._get_client(), "post", mock_post):
        resp = await provider.chat([LLMMessage(role="user", content="hi")])

    assert resp.content == "ok"
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_exhausts_retries_on_persistent_500():
    """Should raise LLMProviderError after all retries exhausted on 500."""
    provider = _make_provider(max_retries=2)
    mock_post = AsyncMock(
        side_effect=[_retryable_response(500), _retryable_response(500), _retryable_response(500)]
    )

    with patch.object(provider._get_client(), "post", mock_post):
        with pytest.raises(LLMProviderError, match="HTTP 500"):
            await provider.chat([LLMMessage(role="user", content="hi")])

    assert mock_post.call_count == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_exhausts_retries_on_network_error():
    """Should raise LLMProviderError after all retries exhausted on network error."""
    provider = _make_provider(max_retries=2)
    mock_post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    with patch.object(provider._get_client(), "post", mock_post):
        with pytest.raises(LLMProviderError, match="after 3 attempts"):
            await provider.chat([LLMMessage(role="user", content="hi")])

    assert mock_post.call_count == 3


@pytest.mark.asyncio
async def test_no_retry_on_400():
    """400 (bad request) should NOT be retried — it's a client error."""
    provider = _make_provider()
    mock_post = AsyncMock(return_value=_retryable_response(400))

    with patch.object(provider._get_client(), "post", mock_post):
        with pytest.raises(LLMProviderError, match="HTTP 400"):
            await provider.chat([LLMMessage(role="user", content="hi")])

    assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_401():
    """401 (unauthorized) should NOT be retried."""
    provider = _make_provider()
    mock_post = AsyncMock(return_value=_retryable_response(401))

    with patch.object(provider._get_client(), "post", mock_post):
        with pytest.raises(LLMProviderError, match="HTTP 401"):
            await provider.chat([LLMMessage(role="user", content="hi")])

    assert mock_post.call_count == 1


# ---- Retry-After header tests ----


@pytest.mark.asyncio
async def test_respects_retry_after_header():
    """429 with Retry-After header should wait that many seconds."""
    provider = _make_provider()
    mock_post = AsyncMock(
        side_effect=[
            _retryable_response(429, headers={"retry-after": "0.01"}),
            _ok_response(),
        ]
    )

    with patch.object(provider._get_client(), "post", mock_post):
        with patch("backend.engines.llm.openai_compatible.asyncio.sleep") as mock_sleep:
            resp = await provider.chat([LLMMessage(role="user", content="hi")])
            assert resp.content == "ok"
            assert mock_sleep.call_count == 1
            delay = mock_sleep.call_args[0][0]
            assert delay == pytest.approx(0.01, abs=0.01)


# ---- Zero retries config ----


@pytest.mark.asyncio
async def test_zero_retries_means_no_retry():
    """max_retries=0 means no retries at all."""
    provider = _make_provider(max_retries=0)
    mock_post = AsyncMock(return_value=_retryable_response(500))

    with patch.object(provider._get_client(), "post", mock_post):
        with pytest.raises(LLMProviderError, match="HTTP 500"):
            await provider.chat([LLMMessage(role="user", content="hi")])

    assert mock_post.call_count == 1


# ---- Backoff delay calculation tests ----


def test_retry_delay_exponential():
    """Delay should roughly double with each attempt."""
    provider = _make_provider(retry_base_delay=1.0, retry_max_delay=60.0)
    d0 = provider._retry_delay(0)
    d1 = provider._retry_delay(1)
    d2 = provider._retry_delay(2)
    # With jitter the actual values vary, but the base should roughly double.
    # d0 ≈ 1.0 ± 0.5, d1 ≈ 2.0 ± 1.0, d2 ≈ 4.0 ± 2.0
    assert d0 < d1 < d2


def test_retry_delay_capped():
    """Delay should not exceed max_delay."""
    provider = _make_provider(retry_base_delay=10.0, retry_max_delay=5.0)
    delay = provider._retry_delay(10)  # large attempt
    assert delay <= 5.0


# ---- Streaming retry tests ----


@pytest.mark.asyncio
async def test_stream_retries_on_initial_500():
    """Stream should retry on initial 500 before the stream is established."""
    provider = _make_provider()

    call_count = 0

    class ErrorContextManager:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def status_code(self):
            return 500

        @property
        def headers(self):
            return {}

        async def aread(self):
            pass

    class OKContextManager:
        def __init__(self):
            self.status_code = 200
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def aiter_lines(self):
            return _async_iter(
                [
                    'data: {"choices":[{"delta":{"content":"hi"}}]}',
                    "data: [DONE]",
                ]
            )

    def mock_stream(method, url, json=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ErrorContextManager()
        return OKContextManager()

    client = provider._get_client()
    client.stream = mock_stream

    chunks = []
    async for chunk in provider.stream_chat([LLMMessage(role="user", content="hi")]):
        chunks.append(chunk)

    assert call_count == 2
    assert any(c.delta == "hi" for c in chunks)
    assert any(c.done for c in chunks)


async def _async_iter(items):
    for item in items:
        yield item
