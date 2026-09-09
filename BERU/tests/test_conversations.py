"""Tests for conversation listing and deletion."""

from __future__ import annotations


async def _new_conversation(client, message: str) -> str:
    resp = await client.post("/api/v1/chat", json={"message": message})
    return resp.json()["conversation_id"]


async def test_list_conversations(client):
    await _new_conversation(client, "one")
    await _new_conversation(client, "two")

    listing = await client.get("/api/v1/conversations")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] >= 2
    assert len(body["items"]) >= 2


async def test_delete_conversation(client):
    conversation_id = await _new_conversation(client, "to be deleted")

    deleted = await client.delete(f"/api/v1/conversations/{conversation_id}")
    assert deleted.status_code == 204

    missing = await client.get(f"/api/v1/conversations/{conversation_id}")
    assert missing.status_code == 404
