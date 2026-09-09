"""LLM provider abstraction.

Everything above this layer speaks in terms of :class:`LLMProvider`,
:class:`LLMMessage`, and :class:`LLMResponse` — never a concrete SDK. Swapping
providers is therefore a configuration change, not a code change.
"""

from backend.engines.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMUsage
from backend.engines.llm.registry import build_provider, get_llm_provider

__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "build_provider",
    "get_llm_provider",
]
