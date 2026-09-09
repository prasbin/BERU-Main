"""Intelligence engine — the orchestration seam above the agents.

Given an agent name and conversation context, it resolves the agent from the
registry and runs generation through the configured LLM provider. It is
deliberately database-agnostic: persistence and memory assembly live in the
service layer, so the engine stays a pure reasoning orchestrator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from backend.agents.base import AgentRequest, AgentResult, AgentStreamChunk, BaseAgent
from backend.agents.registry import AgentRegistry, get_agent_registry
from backend.core.config import Settings
from backend.engines.llm.base import LLMMessage, LLMProvider, ToolCall
from backend.engines.llm.registry import get_llm_provider
from backend.tools.base import ToolResult


class IntelligenceEngine:
    def __init__(self, provider: LLMProvider, agent_registry: AgentRegistry) -> None:
        self._provider = provider
        self._registry = agent_registry

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def resolve_agent(self, agent_name: str | None) -> BaseAgent:
        """Resolve an agent by name (``None`` -> default).

        Exposed so callers can validate an agent *before* starting a streaming
        response, turning an unknown agent into a clean error rather than a
        mid-stream failure.

        Raises:
            backend.core.errors.NotFoundError: if the agent is unknown.
        """
        return self._registry.get(agent_name)

    async def generate(
        self,
        *,
        agent_name: str | None,
        history: list[LLMMessage],
        user_message: str,
        settings: Settings,
    ) -> AgentResult:
        agent = self._registry.get(agent_name)
        request = AgentRequest(
            user_message=user_message,
            history=history,
            provider=self._provider,
            settings=settings,
        )
        return await agent.generate(request)

    async def stream_generate(
        self,
        *,
        agent_name: str | None,
        history: list[LLMMessage],
        user_message: str,
        settings: Settings,
    ) -> AsyncIterator[AgentStreamChunk]:
        """Stream a reply from the resolved agent (mirrors :meth:`generate`)."""
        agent = self._registry.get(agent_name)
        request = AgentRequest(
            user_message=user_message,
            history=history,
            provider=self._provider,
            settings=settings,
        )
        async for chunk in agent.stream(request):
            yield chunk

    async def execute_tool_call(
        self,
        *,
        agent_name: str | None,
        tool_call: ToolCall,
        confirm: bool = False,
    ) -> ToolResult:
        """Resolve the agent and execute one explicit tool call.

        The confirmation flow uses this to run a previously pending (approval
        gated) tool with ``confirm=True`` once the user has approved it.
        """
        agent = self._registry.get(agent_name)
        return await agent.run_tool(tool_call, confirm=confirm, agent_name=agent_name)

    async def continue_after_tool(
        self,
        *,
        agent_name: str | None,
        history: list[LLMMessage],
        user_message: str,
        tool_call: ToolCall,
        tool_result_text: str,
        settings: Settings,
    ) -> AgentResult:
        """Let the agent conclude a turn after a confirmed tool has executed.

        Used by the confirmation flow: the tool has already run, so the agent is
        asked to produce the final reply grounded in the tool result (no further
        tool calls on this continuation).
        """
        agent = self._registry.get(agent_name)
        request = AgentRequest(
            user_message=user_message,
            history=history,
            provider=self._provider,
            settings=settings,
        )
        return await agent.respond_after_tool(request, tool_call, tool_result_text)


@lru_cache
def get_intelligence_engine() -> IntelligenceEngine:
    """Return the process-wide intelligence engine, wired from the registries."""
    return IntelligenceEngine(get_llm_provider(), get_agent_registry())
