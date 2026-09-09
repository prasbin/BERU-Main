"""Tests for the laptop <-> phone sync system: devices, deltas, push mailbox."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.engines.sync import SyncEngine
from backend.models.conversation import Conversation
from backend.models.message import Message
from backend.services.sync_service import SyncService, message_to_dict

# ---- SyncEngine: device registry ----


def test_engine_register_device():
    engine = SyncEngine()
    device = engine.register_device(name="Pixel 9")
    assert device.device_id
    assert device.platform == "android"
    assert device.cursor == 0
    assert engine.device_count() == 1


def test_engine_register_with_capabilities():
    engine = SyncEngine()
    device = engine.register_device(
        name="Phone", capabilities=["notifications", "voice"], push_url="https://push.example/x"
    )
    assert device.capabilities == ["notifications", "voice"]
    assert device.push_url == "https://push.example/x"
    assert device.to_dict()["capabilities"] == ["notifications", "voice"]


def test_engine_get_unknown_device():
    assert SyncEngine().get_device("missing") is None


def test_engine_list_and_revoke():
    engine = SyncEngine()
    device = engine.register_device(name="A")
    engine.register_device(name="B")
    assert len(engine.list_devices()) == 2
    assert engine.revoke_device(device.device_id) is True
    assert engine.revoke_device(device.device_id) is False
    assert engine.device_count() == 1
    assert engine.list_devices()[0]["name"] == "B"


# ---- SyncEngine: cursors ----


def test_engine_set_cursor_monotonic():
    engine = SyncEngine()
    device = engine.register_device(name="Phone")
    assert engine.set_cursor(device.device_id, 100) is True
    assert engine.set_cursor(device.device_id, 50) is True
    assert engine.get_device(device.device_id).cursor == 100
    assert engine.set_cursor("missing", 100) is False


def test_engine_record_activity_updates_last_seen():
    engine = SyncEngine()
    device = engine.register_device(name="Phone")
    assert device.last_seen == device.created_at  # within same instant create
    assert engine.record_activity("missing") is False


# ---- SyncEngine: push mailbox ----


def test_engine_enqueue_and_mailbox():
    engine = SyncEngine()
    device = engine.register_device(name="Phone")
    notification = engine.enqueue_notification(device.device_id, "Reminder", "Drink water")
    assert notification is not None
    assert notification.title == "Reminder"
    pending = engine.mailbox(device.device_id)
    assert len(pending) == 1
    # Reading does not clear the mailbox.
    assert len(engine.mailbox(device.device_id)) == 1


def test_engine_enqueue_unknown_device():
    assert SyncEngine().enqueue_notification("missing", "t", "m") is None


def test_engine_ack_notifications():
    engine = SyncEngine()
    device = engine.register_device(name="Phone")
    n1 = engine.enqueue_notification(device.device_id, "A", "one")
    n2 = engine.enqueue_notification(device.device_id, "B", "two")
    assert engine.ack_notifications(device.device_id, [n1.id]) == 1
    remaining = engine.mailbox(device.device_id)
    assert [n.id for n in remaining] == [n2.id]
    # Acking unknown ids / unknown device is a no-op.
    assert engine.ack_notifications(device.device_id, ["nope"]) == 0
    assert engine.ack_notifications("missing", [n2.id]) == 0


def test_engine_revoke_clears_mailbox():
    engine = SyncEngine()
    device = engine.register_device(name="Phone")
    engine.enqueue_notification(device.device_id, "T", "M")
    engine.revoke_device(device.device_id)
    assert engine.mailbox(device.device_id) == []


def test_engine_status():
    engine = SyncEngine()
    engine.register_device(name="Phone", platform="android")
    engine.register_device(name="Tablet", platform="android")
    status = engine.status()
    assert status["devices"] == 2
    assert status["platforms"] == ["android"]


# ---- SyncService: deltas (DB-backed) ----


async def test_service_pull_initial_sync(db_session):
    conversation = Conversation(agent="beru_core", title="Trip planning")
    db_session.add(conversation)
    await db_session.flush()
    message = Message(conversation_id=conversation.id, role="user", content="pack sunscreen")
    db_session.add(message)
    await db_session.flush()

    service = SyncService()
    device = service.engine.register_device(name="Phone")
    result = await service.pull(db_session, device.device_id)

    assert len(result["messages"]) == 1
    assert result["messages"][0]["content"] == "pack sunscreen"
    assert result["conversations"][0]["id"] == conversation.id
    assert result["cursor"] > 0
    assert device.cursor == result["cursor"]


async def test_service_pull_only_newer_than_cursor(db_session):
    conversation = Conversation(agent="beru_core")
    db_session.add(conversation)
    await db_session.flush()

    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            content="first",
            created_at=t0,
        )
    )
    await db_session.flush()

    service = SyncService()
    device = service.engine.register_device(name="Phone")
    first = await service.pull(db_session, device.device_id)
    assert len(first["messages"]) == 1

    t1 = t0 + timedelta(seconds=1)
    db_session.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content="second",
            created_at=t1,
        )
    )
    await db_session.flush()

    second = await service.pull(db_session, device.device_id)
    assert [m["content"] for m in second["messages"]] == ["second"]
    assert len(second["conversations"]) == 1


async def test_service_pull_no_new_messages(db_session):
    conversation = Conversation(agent="beru_core")
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(conversation_id=conversation.id, role="user", content="hi"))
    await db_session.flush()

    service = SyncService()
    device = service.engine.register_device(name="Phone")
    await service.pull(db_session, device.device_id)
    cursor = device.cursor

    again = await service.pull(db_session, device.device_id)
    assert again["messages"] == []
    assert again["cursor"] == cursor


async def test_service_pull_respects_limit(db_session):
    conversation = Conversation(agent="beru_core")
    db_session.add(conversation)
    await db_session.flush()
    for i in range(5):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content=f"msg {i}",
                created_at=datetime.now(timezone.utc) + timedelta(microseconds=i),
            )
        )
    await db_session.flush()

    service = SyncService()
    device = service.engine.register_device(name="Phone")
    result = await service.pull(db_session, device.device_id, limit=3)
    assert len(result["messages"]) == 3
    # Newest messages win the limit.
    assert result["messages"][-1]["content"] == "msg 4"


async def test_service_pull_unknown_device(db_session):
    service = SyncService()
    with pytest.raises(Exception) as excinfo:
        await service.pull(db_session, "missing")
    assert "Unknown sync device" in str(excinfo.value)


async def test_service_push_message(db_session):
    conversation = Conversation(agent="beru_core", title="Notes")
    db_session.add(conversation)
    await db_session.commit()

    service = SyncService()
    device = service.engine.register_device(name="Phone")
    record = await service.push_message(
        db_session, device.device_id, conversation_id=conversation.id, content="typed on phone"
    )
    assert record["role"] == "user"
    assert record["content"] == "typed on phone"
    assert record["conversation_id"] == conversation.id
    # Pushing does not advance the cursor; pulls are the ordering truth.
    assert device.cursor == 0


async def test_service_push_message_unknown_conversation(db_session):
    service = SyncService()
    device = service.engine.register_device(name="Phone")
    with pytest.raises(Exception) as excinfo:
        await service.push_message(
            db_session, device.device_id, conversation_id="nope", content="x"
        )
    assert "not found" in str(excinfo.value).lower()


async def test_message_to_dict_shape():
    import uuid

    message = Message(
        id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
        role="user",
        content="hello",
        created_at=datetime.now(timezone.utc),
    )
    data = message_to_dict(message)
    assert set(data) == {"id", "conversation_id", "role", "content", "created_at"}


# ---- API ----


@pytest.fixture(autouse=True)
def _reset_sync_service():
    import backend.api.routers.sync as sync_mod

    original = sync_mod._service
    sync_mod._service = SyncService()
    yield
    sync_mod._service = original


async def test_api_register_device(client):
    response = await client.post("/api/v1/sync/devices", json={"name": "Pixel 9"})
    assert response.status_code == 201
    body = response.json()
    assert body["device_id"]
    assert body["platform"] == "android"
    assert body["cursor"] == 0


async def test_api_list_and_get_devices(client):
    created = await client.post(
        "/api/v1/sync/devices", json={"name": "Phone", "platform": "android"}
    )
    device_id = created.json()["device_id"]

    listed = await client.get("/api/v1/sync/devices")
    assert listed.status_code == 200
    assert any(d["device_id"] == device_id for d in listed.json())

    detail = await client.get(f"/api/v1/sync/devices/{device_id}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Phone"

    missing = await client.get("/api/v1/sync/devices/nope")
    assert missing.status_code == 404


async def test_api_revoke_device(client):
    created = await client.post("/api/v1/sync/devices", json={"name": "Phone"})
    device_id = created.json()["device_id"]
    assert (await client.delete(f"/api/v1/sync/devices/{device_id}")).status_code == 204
    assert (await client.delete(f"/api/v1/sync/devices/{device_id}")).status_code == 404


async def test_api_pull_and_push_roundtrip(client):
    device = (await client.post("/api/v1/sync/devices", json={"name": "Phone"})).json()
    conversation = (
        await client.post("/api/v1/chat", json={"message": "hello from laptop"})
    ).json()

    pushed = await client.post(
        f"/api/v1/sync/devices/{device['device_id']}/messages",
        json={"conversation_id": conversation["conversation_id"], "content": "hi from phone"},
    )
    assert pushed.status_code == 201
    assert pushed.json()["role"] == "user"

    pulled = await client.post(
        f"/api/v1/sync/devices/{device['device_id']}/pull", json={"limit": 50}
    )
    assert pulled.status_code == 200
    body = pulled.json()
    contents = [m["content"] for m in body["messages"]]
    assert "hi from phone" in contents
    assert body["cursor"] > 0


async def test_api_pull_unknown_device(client):
    response = await client.post("/api/v1/sync/devices/nope/pull", json={})
    assert response.status_code == 404


async def test_api_pull_with_explicit_cursor(client):
    device = (await client.post("/api/v1/sync/devices", json={"name": "Phone"})).json()
    await client.post("/api/v1/chat", json={"message": "seed"})
    response = await client.post(
        f"/api/v1/sync/devices/{device['device_id']}/pull", json={"cursor": 10**17}
    )
    assert response.status_code == 200
    assert response.json()["messages"] == []


async def test_api_notify_and_mailbox_ack(client):
    device = (await client.post("/api/v1/sync/devices", json={"name": "Phone"})).json()
    n1 = await client.post(
        f"/api/v1/sync/devices/{device['device_id']}/notify",
        json={"title": "Reminder", "message": "stand up"},
    )
    assert n1.status_code == 201
    await client.post(
        f"/api/v1/sync/devices/{device['device_id']}/notify",
        json={"title": "Second", "message": "stretch"},
    )

    mailbox = await client.get(f"/api/v1/sync/devices/{device['device_id']}/mailbox")
    items = mailbox.json()
    assert len(items) == 2
    assert {n["title"] for n in items} == {"Reminder", "Second"}

    ack = await client.post(
        f"/api/v1/sync/devices/{device['device_id']}/mailbox/ack",
        json={"ids": [n1.json()["id"]]},
    )
    assert ack.json()["acknowledged"] == 1
    remaining = await client.get(f"/api/v1/sync/devices/{device['device_id']}/mailbox")
    assert len(remaining.json()) == 1


async def test_api_notify_unknown_device(client):
    response = await client.post(
        "/api/v1/sync/devices/nope/notify", json={"title": "T", "message": "M"}
    )
    assert response.status_code == 404


async def test_api_sync_status(client):
    await client.post("/api/v1/sync/devices", json={"name": "Phone"})
    response = await client.get("/api/v1/sync/status")
    assert response.status_code == 200
    assert response.json()["devices"] == 1