"""Shared FastAPI dependencies.

These assemble the service objects from the process-wide singletons (engine,
provider, registries) while leaving the request-scoped database session to be
injected separately. Tests can override any of these via
``app.dependency_overrides``.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from backend.core.config import get_settings
from backend.engines.embeddings.registry import get_embedding_provider
from backend.engines.intelligence import IntelligenceEngine, get_intelligence_engine
from backend.engines.llm.registry import get_llm_provider
from backend.memory.base import BaseMemory
from backend.memory.conversation_memory import ConversationMemory
from backend.memory.semantic import SemanticMemory
from backend.memory.summarising import SummarisingMemory
from backend.services.chat_service import ChatService
from backend.services.conversation_service import ConversationService


@lru_cache
def get_conversation_service() -> ConversationService:
    return ConversationService()


@lru_cache
def get_memory() -> BaseMemory:
    settings = get_settings()
    if settings.memory_strategy == "semantic":
        embedder = get_embedding_provider()
        return SemanticMemory(
            embedder,
            recent_limit=settings.memory_window_size,
        )
    if settings.memory_strategy == "summarise":
        provider = get_llm_provider()
        return SummarisingMemory(
            provider,
            keep=settings.memory_summary_keep,
            threshold=settings.memory_summary_threshold,
        )
    return ConversationMemory()


def get_chat_service(
    engine: IntelligenceEngine = Depends(get_intelligence_engine),
) -> ChatService:
    return ChatService(engine, get_conversation_service(), get_memory())
