"""LLM provider factory / registry.

Selecting a provider is a pure function of configuration: the built-in
providers below are the fallback, and any additional provider registered as a
``beru.llm_providers`` entry point extends the selectable set (see
:mod:`backend.plugins.discovery`). The chosen provider is cached as a
process-wide singleton via :func:`get_llm_provider`; tests and the app
lifespan can reset the cache when needed.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from backend.core.config import Settings, get_settings
from backend.core.errors import ConfigurationError
from backend.core.logging import get_logger
from backend.engines.llm.base import LLMProvider
from backend.engines.llm.mock import MockProvider
from backend.engines.llm.openai_compatible import OpenAICompatibleProvider
from backend.plugins import discovery

logger = get_logger(__name__)

# Known built-in provider identifiers -> human description.
SUPPORTED_PROVIDERS = {
    "mock": "Deterministic offline provider (no network, no key).",
    "openai_compatible": "Any OpenAI-compatible /chat/completions endpoint.",
}


def _openai_compatible_factory(settings: Settings) -> OpenAICompatibleProvider:
    """Build the OpenAI-compatible provider, validating required settings."""
    if not settings.llm_base_url:
        raise ConfigurationError("LLM_BASE_URL is required for openai_compatible provider.")
    if not settings.llm_model:
        raise ConfigurationError("LLM_MODEL is required for openai_compatible provider.")
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
    )


BUILTIN_FACTORIES: dict[str, Callable[[Settings], LLMProvider]] = {
    "mock": lambda settings: MockProvider(model=settings.llm_model or "beru-mock-1"),
    "openai_compatible": _openai_compatible_factory,
}


def build_provider(settings: Settings) -> LLMProvider:
    """Construct a provider from settings (uncached). Raises on misconfiguration."""
    providers = discovery.merge_with_builtins(
        discovery.GROUP_LLM, BUILTIN_FACTORIES
    )
    name = settings.llm_provider.lower().strip()
    factory = providers.get(name)
    if factory is None:
        raise ConfigurationError(
            f"Unknown LLM_PROVIDER '{settings.llm_provider}'. "
            f"Supported: {', '.join(sorted(providers))}."
        )
    provider = factory(settings)
    if not isinstance(provider, LLMProvider):
        raise ConfigurationError(
            f"Provider '{name}' did not construct an LLMProvider "
            f"(got {type(provider).__name__})."
        )
    return provider


@lru_cache
def get_llm_provider() -> LLMProvider:
    """Return the cached provider selected by the current settings."""
    settings = get_settings()
    provider = build_provider(settings)
    logger.info("LLM provider initialised: %s", provider.name)
    return provider


def reset_llm_provider() -> None:
    """Clear the cached provider (used by tests / hot-reconfiguration)."""
    get_llm_provider.cache_clear()