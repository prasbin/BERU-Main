"""Agent interface and the shared generation flow.

``BaseAgent`` provides a reusable :meth:`generate` implementation (system prompt
+ history + current message -> provider call), so concrete agents usually only
need to declare identity and a system prompt. Agents that need tool use or
multi-step planning can override :meth:`generate` later.

The tool-calling loop lives here: when tools are registered with the agent,
:meth:`generate` (and :meth:`stream`) send them to the LLM and iterate on tool
calls until the LLM produces a final text reply or a max-iterations limit is
reached.

The loop is fully wired to real execution:

1. The LLM emits :class:`ToolCall` values.
2. Each call passes through :meth:`run_tool`, which enforces the agent's
   :class:`PermissionPolicy` and then executes the actual tool.
3. Tools that declare ``requires_confirmation`` are **not** executed on the
   first request; instead the tool's intended call is recorded as a
   :class:`PendingConfirmation` and the model is told permission is required.
   The caller (API layer) can later execute it with ``confirm=True`` via the
   explicit confirmation flow.
4. Tool outcomes feed back to the model as ``tool`` role messages and the loop
   continues until a plain-text reply is produced.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from backend.core.config import Settings
from backend.core.logging import request_id_ctx
from backend.engines.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMUsage,
    ToolCall,
    ToolDefinition,
)
from backend.services.activity_ledger import (
    ActivityEntry,
    args_summary,
    error_summary,
    get_activity_ledger,
)
from backend.tools.base import Tool, ToolResult, serialize_tool_result
from backend.tools.policy import PermissionPolicy, default_policy

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_ITERATIONS = 10


def _validate_arguments(tool: Tool, args: dict) -> list[str]:
    """Lightweight validation of tool arguments against ``tool.parameters``.

    Enforces the declared ``required`` keys and basic type constraints before a
    tool runs, so a malformed/malicious argument payload can't reach an engine
    with an unexpected shape (e.g. a list passed where a path string is
    expected). Extra keys are tolerated (tools ignore what they don't use);
    declared types are validated only for the JSON-schema primitives BERU uses.
    """
    params = tool.parameters or {}
    props = params.get("properties") or {}
    errors: list[str] = []
    for name in params.get("required") or []:
        if name not in args:
            errors.append(f"missing required argument '{name}'")
    for name, value in args.items():
        schema = props.get(name)
        if schema is None:
            continue  # extra parameter — tools ignore what they don't consume
        expected = schema.get("type")
        ok = True
        if expected == "string":
            ok = isinstance(value, str) and not isinstance(value, bool)
        elif expected == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif expected == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif expected == "boolean":
            ok = isinstance(value, bool)
        elif expected == "array":
            ok = isinstance(value, list)
        elif expected == "object":
            ok = isinstance(value, dict)
        if not ok:
            errors.append(f"argument '{name}' must be of type {expected!r}")
    return errors


@dataclass
class AgentRequest:
    """Everything an agent needs to produce a reply.

    ``history`` is prior context (excluding the current message); the current
    turn is passed separately as ``user_message``.
    """

    user_message: str
    history: list[LLMMessage]
    provider: LLMProvider
    settings: Settings


@dataclass
class PendingConfirmation:
    """A tool call that awaits explicit user confirmation.

    Created when the model requests a ``requires_confirmation`` tool and the
    call is not yet approved. The arguments are pre-parsed so the confirmation
    flow can re-invoke the identical tool call without trusting new input.
    """

    tool_name: str
    arguments: dict
    tool_call_id: str


@dataclass
class AgentResult:
    content: str
    agent: str
    model: str | None = None
    usage: LLMUsage | None = None
    tool_calls_made: int = 0
    pending_confirmations: list[PendingConfirmation] = field(default_factory=list)


@dataclass
class AgentStreamChunk:
    """One incremental piece of an agent's streamed reply.

    Mirrors the provider-level chunk but is expressed in agent terms (it carries
    the resolved agent name), so the service/API layers never reach past the
    agent abstraction. The terminal chunk (``done=True``) carries the final
    model/usage plus the tool-work summary for the turn, including any tool
    calls that require user confirmation.
    """

    agent: str
    delta: str = ""
    done: bool = False
    model: str | None = None
    usage: LLMUsage | None = None
    finish_reason: str | None = None
    tool_calls_made: int = 0
    pending_confirmations: list[PendingConfirmation] = field(default_factory=list)


class BaseAgent:
    """Base class for all agents.

    Subclasses set :attr:`name`, :attr:`description`, :attr:`capabilities`, and
    :attr:`default_system_prompt`.
    """

    name: str = "base"
    description: str = "Base agent."
    # Declared, read-only capability tags. Subclasses override with their own list.
    capabilities: list[str] = []
    default_system_prompt: str = "You are a helpful AI assistant."

    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._policy = policy or default_policy()

    def register_tool(self, tool: Tool) -> None:
        """Register a tool that this agent can use."""
        self._tools[tool.name] = tool

    @property
    def tool_definitions(self) -> list[ToolDefinition]:
        """Return tool definitions in LLM function-calling format.

        Only tools that are available or limited are included; unavailable
        tools are never presented to the model.
        """
        return [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self._tools.values()
            if tool.availability != "unavailable"
        ]

    def list_tools(self) -> list[Tool]:
        """Return the agent's registered tool objects (for capability panels)."""
        return list(self._tools.values())

    def resolve_system_prompt(self, settings: Settings) -> str:
        """Global override (BERU_SYSTEM_PROMPT) wins over the agent default."""
        return settings.beru_system_prompt or self.default_system_prompt

    def build_messages(self, request: AgentRequest) -> list[LLMMessage]:
        messages: list[LLMMessage] = []
        system_prompt = self.resolve_system_prompt(request.settings)
        if system_prompt:
            messages.append(LLMMessage(role="system", content=system_prompt))
        messages.extend(request.history)
        messages.append(LLMMessage(role="user", content=request.user_message))
        return messages

    async def run_tool(
        self,
        tool_call: ToolCall,
        *,
        confirm: bool = False,
        agent_name: str = "",
    ) -> ToolResult:
        """Resolve, permission-check, and execute a tool call.

        Returns a structured :class:`ToolResult` — never raises for a tool's own
        failure. Confirmation-gated tools are only executed when ``confirm`` is
        True; otherwise a ``confirmation_required`` result is returned and the
        intended call is *not* run.
        """
        tool = self._tools.get(tool_call.name)
        if tool is None:
            self._record_activity(agent_name, tool_call.name, {}, "failure", None, "Unknown tool")
            return ToolResult.failure(f"Unknown tool: {tool_call.name}")

        # Enforce the permission policy before anything else.
        missing = self._policy.missing_for(agent_name, tool.permissions)
        if missing:
            self._record_activity(
                agent_name, tool_call.name, {}, "permission_denied", None,
                f"Permission denied: missing {', '.join(missing)}",
            )
            return ToolResult.permission_denied(missing)

        # Sensitive tools require explicit confirmation — never auto-confirm.
        if tool.requires_confirmation and not confirm:
            result = ToolResult.confirmation_required(tool.name)
            self._record_activity(
                agent_name, tool_call.name, {}, "confirmation_required", None, result.error,
            )
            return result

        try:
            args = json.loads(tool_call.arguments) if tool_call.arguments else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except json.JSONDecodeError:
            self._record_activity(
                agent_name, tool_call.name, {}, "failure", None, "Invalid JSON arguments"
            )
            return ToolResult.failure(f"Invalid JSON arguments: {tool_call.arguments}")
        except ValueError as exc:
            self._record_activity(
                agent_name, tool_call.name, {}, "failure", None, str(exc)
            )
            return ToolResult.failure(f"Invalid tool arguments: {exc}")

        invalid = _validate_arguments(tool, args)
        if invalid:
            message = f"Invalid arguments for {tool_call.name!r}: {'; '.join(invalid)}"
            self._record_activity(
                agent_name, tool_call.name, args, "failure", None, message
            )
            return ToolResult.failure(message)

        started = time.perf_counter()
        try:
            result = await tool.run(**args)
        except Exception as exc:  # noqa: BLE001 - convert any tool failure into a result
            logger.exception("Tool '%s' raised while executing", tool_call.name)
            result = ToolResult.failure(str(exc))

        outcome = (
            "success" if result.ok
            else "confirmation_required"
            if (isinstance(result.output, dict) and result.output.get("confirmation_required"))
            else "failure"
        )
        self._record_activity(
            agent_name,
            tool_call.name,
            args,
            outcome,
            (time.perf_counter() - started) * 1000,
            result.error,
        )

        return result

    def _record_activity(
        self,
        agent_name: str,
        tool_name: str,
        args: dict,
        outcome: str,
        duration_ms: float | None,
        error: str | None,
    ) -> None:
        """Write one activity entry to the ledger (best-effort)."""
        try:
            get_activity_ledger().record_activity(ActivityEntry(
                timestamp=time.time(),
                request_id=request_id_ctx.get(),
                agent=agent_name,
                tool_name=tool_name,
                args_summary=args_summary(args),
                outcome=outcome,
                duration_ms=round(duration_ms, 1) if duration_ms is not None else None,
                error=error_summary(error),
            ))
        except Exception:
            logger.debug("Could not record activity", exc_info=True)

    async def _execute_tool(
        self, tool_call: ToolCall, *, agent_name: str = "", confirm: bool = False
    ) -> str:
        """Execute a tool call and return its outcome as a feedable JSON string."""
        result = await self.run_tool(tool_call, confirm=confirm, agent_name=agent_name)
        return serialize_tool_result(result, tool_name=tool_call.name)

    def _pending_from(self, tool_call: ToolCall, result_text: str) -> PendingConfirmation | None:
        """When a tool awaits confirmation, capture its intended call.

        Returns ``None`` when the outcome was not a confirmation request.
        """
        try:
            payload = json.loads(result_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not payload.get("confirmation_required"):
            return None
        try:
            args = json.loads(tool_call.arguments) if tool_call.arguments else {}
        except json.JSONDecodeError:
            args = {}
        return PendingConfirmation(
            tool_name=tool_call.name,
            arguments=args,
            tool_call_id=tool_call.id,
        )

    async def generate(self, request: AgentRequest) -> AgentResult:
        """Generate a reply, executing tool calls in a loop if needed.

        When tools are registered, the agent sends them to the LLM. If the LLM
        responds with tool calls, the agent executes them (respecting
        permissions and confirmation gates), appends the results, and asks the
        LLM again — up to ``max_iterations`` times.
        """
        messages = self.build_messages(request)
        tools = self.tool_definitions or None
        max_iter = getattr(request.settings, "max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS)

        total_usage: LLMUsage | None = None
        tool_calls_made = 0
        model: str | None = None
        pending: list[PendingConfirmation] = []

        for _ in range(max_iter):
            response = await request.provider.chat(
                messages,
                temperature=request.settings.llm_temperature,
                max_tokens=request.settings.llm_max_tokens,
                tools=tools,
            )

            model = response.model
            if response.usage:
                total_usage = _accumulate_usage(total_usage, response.usage)

            if not response.tool_calls:
                return AgentResult(
                    content=response.content,
                    agent=self.name,
                    model=model,
                    usage=total_usage,
                    tool_calls_made=tool_calls_made,
                    pending_confirmations=pending,
                )

            # Append the assistant message with tool calls.
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=list(response.tool_calls),
                )
            )

            # Execute each tool call and append results.
            for tc in response.tool_calls:
                tool_calls_made += 1
                result_text = await self._execute_tool(tc, agent_name=self.name)
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=result_text,
                        tool_call_id=tc.id,
                    )
                )
                pending_confirmation = self._pending_from(tc, result_text)
                if pending_confirmation is not None:
                    pending.append(pending_confirmation)

        # Exceeded max iterations — return whatever we have.
        logger.warning("Agent %s hit max tool iterations (%d)", self.name, max_iter)
        return AgentResult(
            content="I've reached the maximum number of tool calls for this request.",
            agent=self.name,
            model=model,
            usage=total_usage,
            tool_calls_made=tool_calls_made,
            pending_confirmations=pending,
        )

    async def stream(self, request: AgentRequest) -> AsyncIterator[AgentStreamChunk]:
        """Stream the reply as :class:`AgentStreamChunk` values.

        Participates in the same tool-calling workflow as :meth:`generate`:
        tool definitions are sent, streamed tool calls are executed as they
        arrive at the end of a stream segment, and the iteration repeats until
        the model answers in text. Text deltas stream to the caller as they are
        produced; a terminal ``done`` chunk carries the usage and any callbacks
        requiring confirmation.
        """
        messages = self.build_messages(request)
        tools = self.tool_definitions or None
        max_iter = getattr(request.settings, "max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS)

        total_usage: LLMUsage | None = None
        tool_calls_made = 0
        model: str | None = None
        pending: list[PendingConfirmation] = []

        for _ in range(max_iter):
            assistant_text: list[str] = []
            tool_calls: list[ToolCall] = []
            finish_reason: str | None = None

            async for chunk in request.provider.stream_chat(
                messages,
                temperature=request.settings.llm_temperature,
                max_tokens=request.settings.llm_max_tokens,
                tools=tools,
            ):
                if chunk.delta:
                    assistant_text.append(chunk.delta)
                    yield AgentStreamChunk(agent=self.name, delta=chunk.delta)
                elif chunk.done:
                    model = chunk.model or model
                    finish_reason = chunk.finish_reason
                    if chunk.usage:
                        total_usage = _accumulate_usage(total_usage, chunk.usage)
                    tool_calls = list(chunk.tool_calls)

            if not tool_calls:
                yield AgentStreamChunk(
                    agent=self.name,
                    done=True,
                    model=model,
                    usage=total_usage,
                    finish_reason=finish_reason,
                    tool_calls_made=tool_calls_made,
                    pending_confirmations=pending,
                )
                return

            # The model invoked tools: record the assistant turn, execute each
            # tool, and continue the loop with the results fed back.
            messages.append(
                LLMMessage(
                    role="assistant",
                    content="".join(assistant_text),
                    tool_calls=tool_calls,
                )
            )
            for tc in tool_calls:
                tool_calls_made += 1
                result_text = await self._execute_tool(tc, agent_name=self.name)
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=result_text,
                        tool_call_id=tc.id,
                    )
                )
                pending_confirmation = self._pending_from(tc, result_text)
                if pending_confirmation is not None:
                    pending.append(pending_confirmation)

        # Exceeded max iterations — emit the fallback reply as streamed text.
        message = "I've reached the maximum number of tool calls for this request."
        yield AgentStreamChunk(agent=self.name, delta=message)
        yield AgentStreamChunk(
            agent=self.name,
            done=True,
            model=model,
            usage=total_usage,
            finish_reason="stop",
            tool_calls_made=tool_calls_made,
            pending_confirmations=pending,
        )

    async def respond_after_tool(
        self,
        request: AgentRequest,
        tool_call: ToolCall,
        tool_result_text: str,
    ) -> AgentResult:
        """Produce the agent's final reply once a confirmed tool has run.

        Builds the continuation transcript — system prompt, prior history, the
        assistant's tool call, its ``tool`` role result, and the original user
        request — and asks the model to conclude without further tool use. This
        is the completion step of the explicit confirmation flow.
        """
        messages: list[LLMMessage] = []
        system_prompt = self.resolve_system_prompt(request.settings)
        if system_prompt:
            messages.append(LLMMessage(role="system", content=system_prompt))
        messages.extend(request.history)
        messages.append(
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[tool_call],
            )
        )
        messages.append(
            LLMMessage(
                role="tool",
                content=tool_result_text,
                tool_call_id=tool_call.id,
            )
        )
        if request.user_message:
            messages.append(LLMMessage(role="user", content=request.user_message))

        response = await request.provider.chat(
            messages,
            temperature=request.settings.llm_temperature,
            max_tokens=request.settings.llm_max_tokens,
        )
        return AgentResult(
            content=response.content or "",
            agent=self.name,
            model=response.model,
            usage=response.usage,
        )


def _accumulate_usage(total: LLMUsage | None, chunk: LLMUsage) -> LLMUsage:
    if total is None:
        return chunk
    return LLMUsage(
        prompt_tokens=total.prompt_tokens + chunk.prompt_tokens,
        completion_tokens=total.completion_tokens + chunk.completion_tokens,
        total_tokens=total.total_tokens + chunk.total_tokens,
    )