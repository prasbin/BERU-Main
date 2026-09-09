"""Provider for any OpenAI-compatible ``/chat/completions`` endpoint.

Works with OpenAI, Groq, OpenRouter, Together, and local servers such as Ollama
and LM Studio — differences are expressed purely through configuration
(``base_url``, ``api_key``, ``model``). The HTTP client is created lazily and
reused across calls.

Retries transient failures (429, 5xx, network errors) with exponential backoff
up to ``max_retries`` attempts. The ``Retry-After`` header from 429 responses
is respected when present.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from backend.core.errors import LLMProviderError
from backend.core.logging import get_logger
from backend.engines.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMStreamChunk,
    LLMUsage,
    ToolCall,
    ToolDefinition,
)

logger = get_logger(__name__)

# HTTP status codes that are safe to retry.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
    ) -> None:
        if not base_url:
            raise LLMProviderError("OpenAI-compatible provider requires a base_url.")
        if not model:
            raise LLMProviderError("OpenAI-compatible provider requires a model.")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url, headers=headers, timeout=self._timeout
            )
        return self._client

    def _retry_delay(self, attempt: int, resp: httpx.Response | None = None) -> float:
        """Compute the delay before the next retry attempt.

        Uses exponential backoff with jitter. If the server provides a
        ``Retry-After`` header (on 429), that value is used instead, clamped
        to ``_retry_max_delay``.
        """
        if resp is not None and resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), self._retry_max_delay)
                except ValueError:
                    pass

        delay = min(
            self._retry_base_delay * (2**attempt),
            self._retry_max_delay,
        )
        jitter = random.uniform(0, delay * 0.5)
        return min(delay + jitter, self._retry_max_delay)

    def _is_retryable(self, exc: Exception | None, status_code: int | None) -> bool:
        """Return True if the error is transient and worth retrying."""
        if status_code is not None and status_code in _RETRYABLE_STATUS_CODES:
            return True
        if exc is not None and isinstance(exc, httpx.TransportError):
            return True
        return False

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[ToolDefinition] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": _serialize_messages(messages),
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            payload["tools"] = _serialize_tool_definitions(tools)
        client = self._get_client()

        for attempt in range(self._max_retries + 1):
            try:
                resp = await client.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "LLM request failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1,
                        self._max_retries + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMProviderError(
                    f"Failed to reach LLM provider after {self._max_retries + 1} attempts: {exc}",
                    detail={"base_url": self._base_url},
                ) from exc

            if resp.status_code >= 400:
                if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    delay = self._retry_delay(attempt, resp)
                    logger.warning(
                        "LLM provider returned HTTP %d (attempt %d/%d) — retrying in %.1fs",
                        resp.status_code,
                        attempt + 1,
                        self._max_retries + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMProviderError(
                    f"LLM provider returned HTTP {resp.status_code}.",
                    detail={"body": _safe_body(resp)},
                )

            try:
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"]["content"] or ""
                finish_reason = choice.get("finish_reason")
                usage_raw = data.get("usage") or {}
                usage = LLMUsage(
                    prompt_tokens=usage_raw.get("prompt_tokens", 0),
                    completion_tokens=usage_raw.get("completion_tokens", 0),
                    total_tokens=usage_raw.get("total_tokens", 0),
                )
                response_tool_calls = _parse_tool_calls(
                    choice["message"].get("tool_calls")
                )
                return LLMResponse(
                    content=content,
                    model=data.get("model", payload["model"]),
                    usage=usage,
                    finish_reason=finish_reason,
                    tool_calls=response_tool_calls,
                    raw=data,
                )
            except (KeyError, IndexError, ValueError) as exc:
                raise LLMProviderError(
                    "Could not parse LLM provider response.",
                ) from exc

        # Should not reach here, but satisfy the type checker.
        raise LLMProviderError("LLM request failed after all retries.")

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[ToolDefinition] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream tokens from an OpenAI-compatible ``/chat/completions`` endpoint.

        Parses Server-Sent Events (``data: {json}`` lines, terminated by
        ``data: [DONE]``), extracting incremental ``choices[0].delta.content``.
        ``stream_options.include_usage`` asks OpenAI-style servers for a final
        usage chunk; servers that ignore it simply report no usage.

        Retries the initial connection on transient errors (429, 5xx, network
        errors) with exponential backoff. Once the stream is established, errors
        are propagated immediately without retry (mid-stream retry would require
        re-sending the full conversation).
        """
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": _serialize_messages(messages),
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = _serialize_tool_definitions(tools)
        client = self._get_client()
        resolved_model = payload["model"]
        usage: LLMUsage | None = None
        finish_reason: str | None = None
        # Streaming tool calls arrive as fragmented deltas (index, id, name,
        # arguments); aggregate the fragments into complete ToolCall values.
        tool_call_parts: dict[int, dict[str, str]] = {}

        for attempt in range(self._max_retries + 1):
            try:
                async with client.stream("POST", "/chat/completions", json=payload) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        retryable = (
                            resp.status_code in _RETRYABLE_STATUS_CODES
                            and attempt < self._max_retries
                        )
                        if retryable:
                            delay = self._retry_delay(attempt, resp)
                            logger.warning(
                                "LLM stream returned HTTP %d (attempt %d/%d) — retrying in %.1fs",
                                resp.status_code,
                                attempt + 1,
                                self._max_retries + 1,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise LLMProviderError(
                            f"LLM provider returned HTTP {resp.status_code}.",
                            detail={"body": _safe_body(resp)},
                        )
                    async for raw_line in resp.aiter_lines():
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:") :].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except ValueError:
                            continue  # tolerate keep-alives / non-JSON comments

                        resolved_model = chunk.get("model") or resolved_model
                        for choice in chunk.get("choices") or []:
                            delta = (choice.get("delta") or {})
                            content = delta.get("content")
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            if content:
                                yield LLMStreamChunk(delta=content)
                            for tc_fragment in delta.get("tool_calls") or []:
                                index = tc_fragment.get("index", 0)
                                part = tool_call_parts.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                part["id"] += tc_fragment.get("id") or ""
                                fn = tc_fragment.get("function") or {}
                                part["name"] += fn.get("name") or ""
                                part["arguments"] += fn.get("arguments") or ""
                        if chunk.get("usage"):
                            u = chunk["usage"]
                            usage = LLMUsage(
                                prompt_tokens=u.get("prompt_tokens", 0),
                                completion_tokens=u.get("completion_tokens", 0),
                                total_tokens=u.get("total_tokens", 0),
                            )
                    break  # stream completed successfully
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "LLM stream failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1,
                        self._max_retries + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMProviderError(
                    f"Failed to reach LLM provider after {self._max_retries + 1} attempts: {exc}",
                    detail={"base_url": self._base_url},
                ) from exc
        else:
            # All retries exhausted on transport errors.
            raise LLMProviderError(
                f"Failed to reach LLM provider after {self._max_retries + 1} attempts.",
                detail={"base_url": self._base_url},
            )

        tool_calls = [
            ToolCall(
                id=parts.get("id") or f"stream_call_{index}",
                name=parts.get("name") or "",
                arguments=parts.get("arguments", ""),
            )
            for index, parts in sorted(tool_call_parts.items())
            if parts.get("name")
        ]
        yield LLMStreamChunk(
            done=True,
            model=resolved_model,
            usage=usage,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _safe_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _serialize_tool_definitions(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    """Convert ToolDefinition dataclasses to the OpenAI tools JSON schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _serialize_messages(messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
    """Convert LLMMessage dataclasses to the OpenAI message JSON format.

    Handles tool_calls on assistant messages and tool_call_id on tool messages,
    which are required for the model to correctly process tool-calling
    multi-turn conversations.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        msg: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments,
                    },
                }
                for tc in m.tool_calls
            ]
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        out.append(msg)
    return out


def _parse_tool_calls(raw_tool_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
    """Parse OpenAI tool_calls from a non-streaming response message."""
    if not raw_tool_calls:
        return []
    result: list[ToolCall] = []
    for tc in raw_tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        result.append(
            ToolCall(
                id=tc.get("id") or f"call_{name}",
                name=name,
                arguments=fn.get("arguments") or "",
            )
        )
    return result
