"""Embedding provider registry — factory + process-wide singleton.

Built-in providers act as the fallback; any provider advertised through the
``beru.embedding_providers`` entry-point group extends the selectable set.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from backend.core.config import Settings, get_settings
from backend.core.errors import ConfigurationError
from backend.core.logging import get_logger
from backend.engines.embeddings.base import EmbeddingProvider
from backend.engines.embeddings.mock import MockEmbeddingProvider
from backend.engines.embeddings.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from backend.plugins import discovery

logger = get_logger(__name__)


def _openai_compatible_factory(
    settings: Settings,
) -> OpenAICompatibleEmbeddingProvider:
    """Build the OpenAI-compatible embedding provider, requiring a base URL."""
    if not settings.embedding_base_url:
        raise ConfigurationError(
            "BERU_EMBEDDING_BASE_URL is required for the openai_compatible embedding provider."
        )
    return OpenAICompatibleEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout=settings.llm_timeout,
    )


BUILTIN_FACTORIES: dict[str, Callable[[Settings], EmbeddingProvider]] = {
    "mock": lambda settings: MockEmbeddingProvider(
        dimension=settings.embedding_dimension
    ),
    "openai_compatible": _openai_compatible_factory,
}


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    providers = discovery.merge_with_builtins(
        discovery.GROUP_EMBEDDINGS, BUILTIN_FACTORIES
    )
    name = settings.embedding_provider.lower().strip()
    factory = providers.get(name)
    if factory is None:
        raise ConfigurationError(
            f"Unknown EMBEDDING_PROVIDER '{settings.embedding_provider}'. "
            f"Supported: {', '.join(sorted(providers))}."
        )
    provider = factory(settings)
    if not isinstance(provider, EmbeddingProvider):
        raise ConfigurationError(
            f"Provider '{name}' did not construct an EmbeddingProvider "
            f"(got {type(provider).__name__})."
        )
    logger.info("Embedding provider: %s", provider.name)
    return provider