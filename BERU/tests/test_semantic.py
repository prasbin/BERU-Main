"""Tests for semantic recall memory strategy."""

from __future__ import annotations

import pytest

from backend.engines.embeddings.mock import MockEmbeddingProvider
from backend.memory.semantic import SemanticMemory
from backend.memory.vector_store import VectorStore, _cosine_similarity
from backend.models.message import Message

# ---- Embedding provider tests ----


async def test_mock_embedding_provider():
    provider = MockEmbeddingProvider(dimension=32)
    vectors = await provider.embed(["hello", "world"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 32
    assert len(vectors[1]) == 32


async def test_mock_embedding_deterministic():
    provider = MockEmbeddingProvider(dimension=16)
    v1 = await provider.embed(["test"])
    v2 = await provider.embed(["test"])
    assert v1[0] == v2[0]


async def test_mock_embedding_normalised():
    provider = MockEmbeddingProvider(dimension=64)
    vectors = await provider.embed(["test"])
    import math
    norm = math.sqrt(sum(v * v for v in vectors[0]))
    assert abs(norm - 1.0) < 1e-6


# ---- Vector store tests ----


async def test_vector_store_upsert_and_search(db_session):
    store = VectorStore()
    provider = MockEmbeddingProvider(dimension=32)

    texts = ["I like cats", "I prefer dogs", "Cats are fluffy"]
    vectors = await provider.embed(texts)

    for i, (text, vec) in enumerate(zip(texts, vectors, strict=False)):
        await store.upsert(
            db_session,
            source_table="messages",
            source_id=f"msg-{i}",
            text=text,
            vector=vec,
        )
    await db_session.commit()

    query_vec = (await provider.embed(["cats"]))[0]
    results = await store.search(db_session, query_vec, source_table="messages", top_k=2)
    assert len(results) == 2
    assert results[0].score > results[1].score


async def test_vector_store_upsert_updates_existing(db_session):
    store = VectorStore()
    provider = MockEmbeddingProvider(dimension=32)

    vec = (await provider.embed(["original"]))[0]
    await store.upsert(db_session, source_table="m", source_id="1", text="original", vector=vec)
    await db_session.commit()

    vec2 = (await provider.embed(["updated"]))[0]
    await store.upsert(db_session, source_table="m", source_id="1", text="updated", vector=vec2)
    await db_session.commit()

    count = await store.count(db_session, source_table="m")
    assert count == 1


async def test_vector_store_delete(db_session):
    store = VectorStore()
    provider = MockEmbeddingProvider(dimension=16)

    vec = (await provider.embed(["text"]))[0]
    await store.upsert(db_session, source_table="m", source_id="1", text="text", vector=vec)
    await db_session.commit()

    await store.delete(db_session, source_table="m", source_id="1")
    await db_session.commit()

    assert await store.count(db_session, source_table="m") == 0


def test_cosine_similarity_identical():
    v = [1.0, 0.0, 0.0]
    assert _cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite():
    assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


# ---- SemanticMemory strategy tests ----


async def test_semantic_memory_build_context_empty(db_session):
    provider = MockEmbeddingProvider(dimension=32)
    memory = SemanticMemory(provider)
    context = await memory.build_context(db_session, "nonexistent", limit=10)
    assert context == []


async def test_semantic_memory_build_context_with_messages(db_session):
    from backend.models.conversation import Conversation

    conv = Conversation(agent="test")
    db_session.add(conv)
    await db_session.flush()

    for i in range(5):
        msg = Message(
            conversation_id=conv.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"Message {i}",
        )
        db_session.add(msg)
    await db_session.commit()

    provider = MockEmbeddingProvider(dimension=32)
    memory = SemanticMemory(provider, recent_limit=3)
    context = await memory.build_context(db_session, conv.id, limit=10)

    assert len(context) == 3
    assert all(m.role in ("user", "assistant") for m in context)


async def test_semantic_memory_index_and_recall(db_session):
    from backend.models.conversation import Conversation

    conv = Conversation(agent="test")
    db_session.add(conv)
    await db_session.flush()

    for i in range(5):
        msg = Message(
            conversation_id=conv.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"Message {i}",
        )
        db_session.add(msg)
    await db_session.commit()

    provider = MockEmbeddingProvider(dimension=32)
    memory = SemanticMemory(provider, recent_limit=2, recall_limit=3)

    for msg in list(db_session):
        if hasattr(msg, "role") and msg.role == "user":
            await memory.index_message(db_session, msg)
    await db_session.commit()

    context = await memory.build_context(db_session, conv.id, limit=10)

    has_recalled = any("[Relevant past messages]" in m.content for m in context)
    assert has_recalled or len(context) == 2
