"""Memory subsystem.

The foundation ships short-term conversation memory (a recent-message window).
Long-term/user/project memory are planned extensions that will implement the
same :class:`BaseMemory` interface (see docs/roadmap.md).
"""

from backend.memory.base import BaseMemory
from backend.memory.conversation_memory import ConversationMemory

__all__ = ["BaseMemory", "ConversationMemory"]
