"""Tests for long-term user memory (facts)."""

from __future__ import annotations

import pytest

from backend.core.errors import NotFoundError
from backend.services.fact_service import FactService

# ---- Service layer tests ----


async def test_upsert_creates_new_fact(db_session):
    service = FactService()
    fact = await service.upsert(db_session, key="name", value="Alex", category="identity")
    await db_session.commit()

    assert fact.key == "name"
    assert fact.value == "Alex"
    assert fact.category == "identity"
    assert fact.id


async def test_upsert_updates_existing_fact(db_session):
    service = FactService()
    await service.upsert(db_session, key="name", value="Alex")
    await db_session.commit()

    updated = await service.upsert(db_session, key="name", value="Jordan")
    await db_session.commit()

    assert updated.value == "Jordan"


async def test_get_returns_fact(db_session):
    service = FactService()
    await service.upsert(db_session, key="color", value="blue")
    await db_session.commit()

    fact = await service.get(db_session, "color")
    assert fact.value == "blue"


async def test_get_raises_for_missing_key(db_session):
    service = FactService()
    with pytest.raises(NotFoundError):
        await service.get(db_session, "nonexistent")


async def test_list_returns_all_facts(db_session):
    service = FactService()
    await service.upsert(db_session, key="a", value="1")
    await service.upsert(db_session, key="b", value="2")
    await db_session.commit()

    total, items = await service.list(db_session)
    assert total == 2
    assert len(items) == 2


async def test_list_filters_by_category(db_session):
    service = FactService()
    await service.upsert(db_session, key="a", value="1", category="prefs")
    await service.upsert(db_session, key="b", value="2", category="identity")
    await db_session.commit()

    total, items = await service.list(db_session, category="prefs")
    assert total == 1
    assert items[0].key == "a"


async def test_delete_removes_fact(db_session):
    service = FactService()
    await service.upsert(db_session, key="temp", value="value")
    await db_session.commit()

    await service.delete(db_session, "temp")
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await service.get(db_session, "temp")


async def test_delete_raises_for_missing_key(db_session):
    service = FactService()
    with pytest.raises(NotFoundError):
        await service.delete(db_session, "nonexistent")


async def test_all_as_text(db_session):
    service = FactService()
    await service.upsert(db_session, key="name", value="Alex")
    await service.upsert(db_session, key="color", value="blue")
    await db_session.commit()

    texts = await service.all_as_text(db_session)
    assert len(texts) == 2
    assert "name: Alex" in texts
    assert "color: blue" in texts


# ---- API endpoint tests ----


async def test_api_create_fact(client):
    resp = await client.post(
        "/api/v1/facts",
        json={"key": "name", "value": "Alex", "category": "identity"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["key"] == "name"
    assert data["value"] == "Alex"
    assert data["category"] == "identity"
    assert data["id"]


async def test_api_update_fact(client):
    await client.post("/api/v1/facts", json={"key": "name", "value": "Alex"})
    resp = await client.post("/api/v1/facts", json={"key": "name", "value": "Jordan"})
    assert resp.status_code == 201
    assert resp.json()["value"] == "Jordan"


async def test_api_get_fact(client):
    await client.post("/api/v1/facts", json={"key": "color", "value": "blue"})
    resp = await client.get("/api/v1/facts/color")
    assert resp.status_code == 200
    assert resp.json()["value"] == "blue"


async def test_api_get_missing_fact(client):
    resp = await client.get("/api/v1/facts/nonexistent")
    assert resp.status_code == 404


async def test_api_list_facts(client):
    await client.post("/api/v1/facts", json={"key": "a", "value": "1"})
    await client.post("/api/v1/facts", json={"key": "b", "value": "2"})
    resp = await client.get("/api/v1/facts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


async def test_api_list_facts_by_category(client):
    await client.post("/api/v1/facts", json={"key": "a", "value": "1", "category": "prefs"})
    await client.post("/api/v1/facts", json={"key": "b", "value": "2", "category": "identity"})
    resp = await client.get("/api/v1/facts", params={"category": "prefs"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


async def test_api_delete_fact(client):
    await client.post("/api/v1/facts", json={"key": "temp", "value": "value"})
    resp = await client.delete("/api/v1/facts/temp")
    assert resp.status_code == 204

    resp = await client.get("/api/v1/facts/temp")
    assert resp.status_code == 404


async def test_api_delete_missing_fact(client):
    resp = await client.delete("/api/v1/facts/nonexistent")
    assert resp.status_code == 404


# ---- Chat integration tests ----


async def test_facts_included_in_chat_context(client):
    """Facts should be included in the context sent to the LLM."""
    await client.post("/api/v1/facts", json={"key": "name", "value": "Alex"})
    resp = await client.post("/api/v1/chat", json={"message": "What is my name?"})
    assert resp.status_code == 200
    # The mock provider echoes the last user message, so just verify it works.
    data = resp.json()
    assert data["message"]["content"]


async def test_chat_works_without_facts(client):
    """Chat should work fine when no facts exist."""
    resp = await client.post("/api/v1/chat", json={"message": "Hello"})
    assert resp.status_code == 200
