"""Deterministic offline LLM provider.

Requires no network access and no API key, so BERU runs out of the box and the
test-suite is fully hermetic. It is not a real model — it echoes the latest user
message and points the operator at how to enable a real provider.

Supports tool calling in mock mode: when tools are provided and the message
history contains a user message, it simulates a tool call on the first iteration
and returns the tool result on the second.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from backend.engines.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMStreamChunk,
    LLMUsage,
    ToolCall,
    ToolDefinition,
)

_MOCK_TOOL_CALL_MARKER = "__BERU_MOCK_TOOL_CALL__"


def _estimate_tokens(text: str) -> int:
    """Rough, deterministic token estimate (~ word count). Good enough for a mock."""
    return len(text.split())


def _mock_content(messages: Sequence[LLMMessage]) -> str:
    """Build the deterministic reply text from the latest user message."""
    last_user = next(
        (m.content for m in reversed(list(messages)) if m.role == "user"),
        "",
    )
    return (
        f"BERU (mock) received: {last_user}\n\n"
        "This is a deterministic offline response. Set LLM_PROVIDER="
        "openai_compatible (with LLM_BASE_URL / LLM_API_KEY / LLM_MODEL) "
        "to enable real reasoning."
    )


def _should_mock_tool_call(
    messages: Sequence[LLMMessage],
    tools: Sequence[ToolDefinition] | None,
) -> bool:
    """Determine if the mock should simulate a tool call.

    Returns True when tools are provided, there's at least one user message,
    and the last non-system message is NOT a tool result (to avoid infinite loops).
    """
    if not tools:
        return False
    has_user = any(m.role == "user" for m in messages)
    last_msg = messages[-1] if messages else None
    has_tool_result = last_msg is not None and last_msg.role == "tool"
    return has_user and not has_tool_result


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, model: str = "beru-mock-1") -> None:
        self._model = model

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
        if _should_mock_tool_call(messages, tools):
            tool = tools[0]
            args = json.dumps({"query": "mock"})
            return LLMResponse(
                content="",
                model=model or self._model,
                usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                tool_calls=[
                    ToolCall(
                        id=f"call_mock_{tool.name}",
                        name=tool.name,
                        arguments=args,
                    )
                ],
                finish_reason="tool_calls",
                raw={"provider": "mock"},
            )

        content = _mock_content(messages)
        prompt_tokens = sum(_estimate_tokens(m.content) for m in messages)
        completion_tokens = _estimate_tokens(content)
        return LLMResponse(
            content=content,
            model=model or self._model,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
            raw={"provider": "mock"},
        )

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
        """Yield the reply word by word, then a terminal chunk with usage.

        The deltas concatenate losslessly back to the full reply, so tests can
        assert that streaming and non-streaming produce identical content.

        Mirrors :meth:`chat`: when tool calling is expected, the stream ends
        after a single terminal chunk carrying the simulated tool call instead
        of text (``finish_reason == "tool_calls"``).
        """
        if _should_mock_tool_call(messages, tools):
            tool = tools[0]
            yield LLMStreamChunk(
                done=True,
                model=model or self._model,
                usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id=f"call_mock_{tool.name}",
                        name=tool.name,
                        arguments=json.dumps({"query": "mock"}),
                    )
                ],
            )
            return

        content = _mock_content(messages)
        prompt_tokens = sum(_estimate_tokens(m.content) for m in messages)
        completion_tokens = _estimate_tokens(content)

        # split(" ") preserves newlines; re-adding single spaces reconstructs
        # the original text exactly.
        for index, word in enumerate(content.split(" ")):
            yield LLMStreamChunk(delta=word if index == 0 else f" {word}")

        yield LLMStreamChunk(
            done=True,
            model=model or self._model,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
        )
