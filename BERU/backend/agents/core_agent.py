"""BERU Core — the default, general-purpose orchestration agent."""

from __future__ import annotations

from backend.agents.base import BaseAgent

CORE_SYSTEM_PROMPT = (
    "You are BERU, a personal AI operating system and assistant. "
    "You are helpful, precise, and honest. Reason carefully, keep answers "
    "grounded, and clearly say when you are unsure. When a request would need a "
    "capability you do not yet have, explain the limitation instead of pretending."
)


class CoreAgent(BaseAgent):
    name = "beru_core"
    description = "General-purpose intelligence and orchestration agent."
    capabilities = ["conversation", "reasoning", "general_knowledge"]
    default_system_prompt = CORE_SYSTEM_PROMPT
