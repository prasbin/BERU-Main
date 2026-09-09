"""Agent subsystem.

Agents are specialised handlers with a name, description, declared capabilities,
and a system prompt. The foundation ships the general-purpose BERU Core agent;
specialised agents (IGRIS, DHANUS, TANK) will register through the same
interface in later stages (see docs/roadmap.md).
"""

from backend.agents.base import AgentRequest, AgentResult, BaseAgent
from backend.agents.core_agent import CoreAgent
from backend.agents.registry import AgentRegistry, get_agent_registry

__all__ = [
    "AgentRequest",
    "AgentResult",
    "BaseAgent",
    "CoreAgent",
    "AgentRegistry",
    "get_agent_registry",
]
