"""Provider-agnostic LLM interface and value types.

These types are deliberately framework-free (plain dataclasses) so the engine
and agent layers never depend on FastAPI, Pydantic, or a vendor SDK.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolDefinition:
    """Description of a tool available to the LLM (OpenAI function-calling format)."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the LLM."""

    id: str
    name: str
    arguments: str = ""


@dataclass(frozen=True)
class LLMMessage:
    """A single chat message in provider-neutral form."""

    role: Role
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class LLMUsage:
    """Token accounting for one generation."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    """The result of a chat completion."""

    content: str
    model: str
    usage: LLMUsage | None = None
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMStreamChunk:
    """One incremental piece of a streaming completion.

    Providers emit zero or more chunks carrying ``delta`` text, followed by
    exactly one terminal chunk with ``done=True`` that carries the final
    ``model``/``usage``/``finish_reason`` when the provider reports them.

    ``tool_calls`` is populated on the terminal chunk when the model decided to
    call tools instead of answering in text (``finish_reason == "tool_calls"``).
    """

    delta: str = ""
    done: bool = False
    model: str | None = None
    usage: LLMUsage | None = None
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(ABC):
    """Abstract base every concrete provider implements.

    Implementations must be safe to share as a process-wide singleton and must
    not raise on construction for missing network resources — validation of
    configuration belongs in the registry/factory.
    """

    #: Stable identifier for the provider (used in config and diagnostics).
    name: str = "base"

    @abstractmethod
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
        """Generate a completion for ``messages`` and return an :class:`LLMResponse`.

        Raises:
            backend.core.errors.LLMProviderError: on upstream failure or when the
                response cannot be parsed.
        """
        raise NotImplementedError

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream a completion as incremental :class:`LLMStreamChunk` values.

        The default implementation degrades gracefully to a single-shot
        :meth:`chat` call — yielding the whole reply as one delta followed by a
        terminal chunk — so providers without native streaming still work
        through the same interface. Providers with real streaming override this.

        Raises:
            backend.core.errors.LLMProviderError: on upstream failure.
        """
        response = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        if response.content:
            yield LLMStreamChunk(delta=response.content)
        yield LLMStreamChunk(
            done=True,
            model=response.model,
            usage=response.usage,
            finish_reason=response.finish_reason,
            tool_calls=list(response.tool_calls),
        )

    async def aclose(self) -> None:
        """Release any held resources (network clients). Safe to call repeatedly."""
        return None

    async def probe(self) -> None:
        """Verify the provider can generate a completion.

        The default implementation performs one minimal generation (a single
        token) so any provider exercises its real request path; providers
        without network access (mock) complete instantly. Raises
        :class:`backend.core.errors.LLMProviderError` on failure.
        """
        await self.chat(
            [LLMMessage(role="user", content="probe")],
            max_tokens=1,
            temperature=0.0,
        )
