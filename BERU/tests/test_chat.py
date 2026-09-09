"""End-to-end chat flow tests: the seven capabilities of the minimum core."""

from __future__ import annotations


async def test_chat_creates_conversation_and_persists(client):
    resp = await client.post("/api/v1/chat", json={"message": "Hello BERU"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["conversation_id"]
    assert body["agent"] == "beru_core"
    assert body["message"]["role"] == "assistant"
    assert "Hello BERU" in body["message"]["content"]  # mock echoes the input
    assert body["usage"]["total_tokens"] >= 0

    conversation_id = body["conversation_id"]

    detail = await client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello BERU"
    assert messages[1]["role"] == "assistant"


async def test_chat_continues_existing_conversation(client):
    first = await client.post("/api/v1/chat", json={"message": "First"})
    conversation_id = first.json()["conversation_id"]

    second = await client.post(
        "/api/v1/chat",
        json={"message": "Second", "conversation_id": conversation_id},
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id

    messages = (await client.get(f"/api/v1/conversations/{conversation_id}/messages")).json()
    assert len(messages) == 4
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]


async def test_chat_rejects_empty_message(client):
    resp = await client.post("/api/v1/chat", json={"message": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "validation_error"


async def test_chat_unknown_agent_returns_404(client):
    resp = await client.post("/api/v1/chat", json={"message": "hi", "agent": "ghost"})
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


async def test_get_missing_conversation_returns_404(client):
    resp = await client.get("/api/v1/conversations/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"
