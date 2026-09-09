"""LLM provider factory / registry.

Selecting a provider is a pure function of configuration. The chosen provider is
cached as a process-wide singleton via :func:`get_llm_provider`; tests and the
app lifespan can reset the cache when needed.
"""

from __future__ import annotations

from functools import lru_cache

from backend.core.config import Settings, get_settings
from backend.core.errors import ConfigurationError
from backend.core.logging import get_logger
from backend.engines.llm.base import LLMProvider
from backend.engines.llm.mock import MockProvider
from backend.engines.llm.openai_compatible import OpenAICompatibleProvider

logger = get_logger(__name__)

# Known provider identifiers -> human description (also used for validation).
SUPPORTED_PROVIDERS = {
    "mock": "Deterministic offline provider (no network, no key).",
    "openai_compatible": "Any OpenAI-compatible /chat/completions endpoint.",
}


def build_provider(settings: Settings) -> LLMProvider:
    """Construct a provider from settings (uncached). Raises on misconfiguration."""
    provider = settings.llm_provider.lower().strip()

    if provider == "mock":
        return MockProvider(model=settings.llm_model or "beru-mock-1")

    if provider == "openai_compatible":
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

    raise ConfigurationError(
        f"Unknown LLM_PROVIDER '{settings.llm_provider}'. "
        f"Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
    )


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
