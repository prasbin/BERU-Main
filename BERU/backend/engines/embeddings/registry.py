"""Embedding provider registry — factory + process-wide singleton."""

from __future__ import annotations

from functools import lru_cache

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.engines.embeddings.base import EmbeddingProvider

logger = get_logger(__name__)


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    provider_name = settings.embedding_provider

    if provider_name == "openai_compatible":
        from backend.engines.embeddings.openai_compatible import (
            OpenAICompatibleEmbeddingProvider,
        )

        provider = OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            timeout=settings.llm_timeout,
        )
        logger.info("Embedding provider: %s (model=%s)", provider.name, settings.embedding_model)
        return provider

    # Default: mock provider (no network, deterministic)
    from backend.engines.embeddings.mock import MockEmbeddingProvider

    provider = MockEmbeddingProvider(dimension=settings.embedding_dimension)
    logger.info(
        "Embedding provider: %s (dimension=%d)",
        provider.name,
        settings.embedding_dimension,
    )
    return provider
