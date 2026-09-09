"""Tests for memory scoping: per-agent and per-project isolation."""

from __future__ import annotations

import pytest

from backend.services.fact_service import FactService
from backend.services.project_service import ProjectService

# ---- Project service tests ----


async def test_create_project(db_session):
    service = ProjectService()
    project = await service.create(db_session, name="test-project", description="A test project")
    await db_session.commit()

    assert project.name == "test-project"
    assert project.description == "A test project"
    assert project.id


async def test_create_duplicate_project_raises(db_session):
    service = ProjectService()
    await service.create(db_session, name="dup")
    await db_session.commit()

    from backend.core.errors import BadRequestError

    with pytest.raises(BadRequestError):
        await service.create(db_session, name="dup")


async def test_get_project(db_session):
    service = ProjectService()
    project = await service.create(db_session, name="get-me")
    await db_session.commit()

    fetched = await service.get(db_session, project.id)
    assert fetched.name == "get-me"


async def test_get_missing_project_raises(db_session):
    service = ProjectService()
    from backend.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await service.get(db_session, "nonexistent")


async def test_list_projects(db_session):
    service = ProjectService()
    await service.create(db_session, name="p1")
    await service.create(db_session, name="p2")
    await db_session.commit()

    total, items = await service.list(db_session)
    assert total == 2
    assert len(items) == 2


async def test_update_project(db_session):
    service = ProjectService()
    project = await service.create(db_session, name="old-name")
    await db_session.commit()

    updated = await service.update(db_session, project.id, name="new-name")
    await db_session.commit()
    assert updated.name == "new-name"


async def test_delete_project(db_session):
    service = ProjectService()
    project = await service.create(db_session, name="delete-me")
    await db_session.commit()

    await service.delete(db_session, project.id)
    await db_session.commit()

    from backend.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await service.get(db_session, project.id)


# ---- Fact scoping tests ----


async def test_fact_scoped_by_agent(db_session):
    service = FactService()
    await service.upsert(db_session, key="a1", value="global", agent=None)
    await service.upsert(db_session, key="a2", value="agent-a", agent="agent_a")
    await service.upsert(db_session, key="a3", value="agent-b", agent="agent_b")
    await db_session.commit()

    # agent=None means "no filter" — returns all facts
    _, all_facts = await service.list(db_session, agent=None)
    assert len(all_facts) == 3

    _, agent_a_facts = await service.list(db_session, agent="agent_a")
    assert len(agent_a_facts) == 1
    assert agent_a_facts[0].value == "agent-a"


async def test_fact_scoped_by_project(db_session):
    project_service = ProjectService()
    p1 = await project_service.create(db_session, name="proj1")
    p2 = await project_service.create(db_session, name="proj2")
    await db_session.commit()

    service = FactService()
    await service.upsert(db_session, key="p1_fact", value="proj1-val", project_id=p1.id)
    await service.upsert(db_session, key="p2_fact", value="proj2-val", project_id=p2.id)
    await db_session.commit()

    _, p1_facts = await service.list(db_session, project_id=p1.id)
    assert len(p1_facts) == 1
    assert p1_facts[0].value == "proj1-val"

    _, p2_facts = await service.list(db_session, project_id=p2.id)
    assert len(p2_facts) == 1
    assert p2_facts[0].value == "proj2-val"


async def test_fact_all_as_text_scoped(db_session):
    service = FactService()
    await service.upsert(db_session, key="global", value="g")
    await service.upsert(db_session, key="scoped", value="s", agent="my_agent")
    await db_session.commit()

    all_facts = await service.all_as_text(db_session, limit=100)
    assert len(all_facts) == 2

    scoped_facts = await service.all_as_text(db_session, agent="my_agent", limit=100)
    assert len(scoped_facts) == 1
    assert "scoped: s" in scoped_facts[0]


# ---- Conversation scoping tests ----


async def test_conversation_has_project_id(db_session):
    from backend.models.conversation import Conversation
    from backend.services.project_service import ProjectService

    ps = ProjectService()
    project = await ps.create(db_session, name="conv-proj")
    await db_session.commit()

    conv = Conversation(agent="test", project_id=project.id)
    db_session.add(conv)
    await db_session.commit()

    assert conv.project_id == project.id


# ---- API endpoint tests ----


async def test_api_create_project(client):
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "my-project", "description": "Test"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my-project"
    assert data["description"] == "Test"
    assert data["id"]


async def test_api_list_projects(client):
    await client.post("/api/v1/projects", json={"name": "p1"})
    await client.post("/api/v1/projects", json={"name": "p2"})
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


async def test_api_get_project(client):
    resp = await client.post("/api/v1/projects", json={"name": "get-proj"})
    project_id = resp.json()["id"]
    resp = await client.get(f"/api/v1/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "get-proj"


async def test_api_update_project(client):
    resp = await client.post("/api/v1/projects", json={"name": "old"})
    project_id = resp.json()["id"]
    resp = await client.patch(
        f"/api/v1/projects/{project_id}",
        json={"name": "new", "description": "Updated"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "new"


async def test_api_delete_project(client):
    resp = await client.post("/api/v1/projects", json={"name": "del-proj"})
    project_id = resp.json()["id"]
    resp = await client.delete(f"/api/v1/projects/{project_id}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/projects/{project_id}")
    assert resp.status_code == 404
