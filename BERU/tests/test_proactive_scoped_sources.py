"""Tests for scoped (parameterised) monitor data sources.

A trigger can reference a registered source with a ``<base>:<scope>`` suffix to
narrow it to a fact category, a project, or a single conversation — e.g.
``memory.facts_count:work``, ``conversations.messages_24h:<conversation_id>``.
Includes coverage that scoping isolates values between categories/projects/
conversations, that scoped fires write correct history, and that pushed values
still drive sources that are not registered as scoped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.engines.monitor import ConditionType, Trigger, TriggerCondition
from backend.models.conversation import Conversation
from backend.models.message import Message
from backend.models.project import Project
from backend.services.fact_service import FactService
from backend.services.proactive_service import (
    get_event_monitor,
    get_scheduler,
    set_session_factory,
)
from backend.services.proactive_store import list_trigger_fires


@pytest_asyncio.fixture
async def _proactive(_sessionmaker):
    """Point the shared proactive runtime at the temp DB and clear its state."""
    set_session_factory(lambda: _sessionmaker)
    get_scheduler().clear()
    get_event_monitor().clear()
    return get_scheduler(), get_event_monitor()


async def _add_project(session, name: str) -> Project:
    project = Project(name=name)
    session.add(project)
    await session.flush()
    return project


async def _add_fact(
    session, key: str, value: str, category: str = "general", project_id: str | None = None
) -> None:
    await FactService().upsert(
        session,
        key=key,
        value=value,
        category=category,
        agent="beru_core",
        project_id=project_id,
    )
    await session.flush()


def _threshold_trigger(name: str, source: str, value, operator: str = "gt") -> Trigger:
    return Trigger(
        name=name,
        condition=TriggerCondition(
            condition_type=ConditionType.THRESHOLD,
            source=source,
            operator=operator,
            value=value,
        ),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_monitor_registers_scoped_sources(_proactive):
    """The shared monitor exposes the scoped source families."""
    _, monitor = _proactive
    sources = set(monitor.list_sources())
    assert {
        "memory.facts_count",
        "memory.facts_project_count",
        "memory.facts_text",
        "memory.facts_project_text",
        "conversations.new_24h",
        "conversations.messages_24h",
        "conversations.project_messages_24h",
    }.issubset(sources)


# ---------------------------------------------------------------------------
# Facts: per-category and per-project counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facts_category_count_scopes_trigger(_proactive, db_session):
    """A category-scoped count trigger only sees that category's facts."""
    _, monitor = _proactive
    await _add_fact(db_session, "work_1", "one", category="work")
    await _add_fact(db_session, "work_2", "two", category="work")
    await _add_fact(db_session, "personal_1", "one", category="personal")
    await db_session.commit()

    work = monitor.add_trigger(_threshold_trigger("w", "memory.facts_count:work", 1))
    personal = monitor.add_trigger(
        _threshold_trigger("p", "memory.facts_count:personal", 1)
    )
    fired = await monitor.check_once()
    assert work.id in fired
    assert personal.id not in fired


@pytest.mark.asyncio
async def test_facts_project_count_scoped(_proactive, db_session):
    """A project-scoped count trigger only sees that project's facts."""
    _, monitor = _proactive
    alpha = await _add_project(db_session, "alpha")
    beta = await _add_project(db_session, "beta")
    await _add_fact(db_session, "a1", "x", project_id=alpha.id)
    await _add_fact(db_session, "a2", "x", project_id=alpha.id)
    await _add_fact(db_session, "a3", "x", project_id=alpha.id)
    await db_session.commit()

    alpha_trig = monitor.add_trigger(
        _threshold_trigger("a", f"memory.facts_project_count:{alpha.id}", 3, operator="gte")
    )
    beta_trig = monitor.add_trigger(
        _threshold_trigger("b", f"memory.facts_project_count:{beta.id}", 1)
    )
    fired = await monitor.check_once()
    assert alpha_trig.id in fired
    assert beta_trig.id not in fired


# ---------------------------------------------------------------------------
# Facts: per-category and per-project text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facts_text_category_scoped(_proactive, db_session):
    """Pattern triggers on category-scoped text only match that category."""
    _, monitor = _proactive
    await _add_fact(db_session, "lang", "TelemetryLabs", category="work")
    await _add_fact(db_session, "hobby", "arcade nights", category="personal")
    await db_session.commit()

    work = monitor.add_trigger(
        Trigger(
            name="work-sample",
            condition=TriggerCondition(
                condition_type=ConditionType.PATTERN,
                source="memory.facts_text:work",
                value="TelemetryLabs",
            ),
        )
    )
    personal = monitor.add_trigger(
        Trigger(
            name="personal-sample",
            condition=TriggerCondition(
                condition_type=ConditionType.PATTERN,
                source="memory.facts_text:personal",
                value="TelemetryLabs",
            ),
        )
    )
    fired = await monitor.check_once()
    assert work.id in fired
    assert personal.id not in fired


@pytest.mark.asyncio
async def test_facts_project_text_scoped(_proactive, db_session):
    """Pattern triggers on project-scoped text only match that project."""
    _, monitor = _proactive
    alpha = await _add_project(db_session, "alpha")
    await _add_fact(db_session, "only_in_alpha", "python", project_id=alpha.id)
    await _add_fact(db_session, "ungrouped", "python", category="general")
    await db_session.commit()

    alpha_trig = monitor.add_trigger(
        Trigger(
            name="alpha-python",
            condition=TriggerCondition(
                condition_type=ConditionType.PATTERN,
                source=f"memory.facts_project_text:{alpha.id}",
                value="python",
            ),
        )
    )
    fired = await monitor.check_once()
    assert alpha_trig.id in fired
    # The fact not owned by the project must not appear in the text's content
    # domain — a pattern with no project match stays quiet.
    ghost = monitor.add_trigger(
        Trigger(
            name="ghost-project",
            condition=TriggerCondition(
                condition_type=ConditionType.PATTERN,
                source="memory.facts_project_text:does-not-exist",
                value="python",
            ),
        )
    )
    fired = await monitor.check_once()
    assert ghost.id not in fired


# ---------------------------------------------------------------------------
# Conversations: per-conversation and per-project activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversations_messages_24h_scoped_to_one_conversation(_proactive, db_session):
    """A per-conversation activity trigger counts only that conversation."""
    _, monitor = _proactive
    busy = Conversation(agent="beru_core", title="busy")
    quiet = Conversation(agent="beru_core", title="quiet")
    db_session.add_all([busy, quiet])
    await db_session.flush()
    for idx in range(3):
        db_session.add(
            Message(conversation_id=busy.id, role="user", content=f"m{idx}")
        )
    await db_session.commit()

    on_busy = monitor.add_trigger(
        _threshold_trigger("b", f"conversations.messages_24h:{busy.id}", 3, operator="gte")
    )
    on_quiet = monitor.add_trigger(
        _threshold_trigger("q", f"conversations.messages_24h:{quiet.id}", 3, operator="gte")
    )
    fired = await monitor.check_once()
    assert on_busy.id in fired
    assert on_quiet.id not in fired


@pytest.mark.asyncio
async def test_conversations_project_new_24h_scoped(_proactive, db_session):
    """Project-scoped 'new conversations' ignores old conversations elsewhere."""
    _, monitor = _proactive
    live = await _add_project(db_session, "live")
    dead = await _add_project(db_session, "dead")

    fresh = Conversation(agent="beru_core", title="fresh", project_id=live.id)
    db_session.add(fresh)
    await db_session.flush()

    old_cutoff = datetime.now(timezone.utc) - timedelta(hours=50)
    stale = Conversation(agent="beru_core", title="stale", project_id=dead.id)
    stale.created_at = old_cutoff
    stale.updated_at = old_cutoff
    db_session.add(stale)
    await db_session.commit()

    on_live = monitor.add_trigger(
        _threshold_trigger("live", f"conversations.new_24h:{live.id}", 1, operator="gte")
    )
    on_dead = monitor.add_trigger(
        _threshold_trigger("dead", f"conversations.new_24h:{dead.id}", 1, operator="gte")
    )
    fired = await monitor.check_once()
    assert on_live.id in fired
    assert on_dead.id not in fired


@pytest.mark.asyncio
async def test_conversations_project_messages_24h_scoped(_proactive, db_session):
    """Project-scoped message volume only sees recent messages in that project."""
    _, monitor = _proactive
    alpha = await _add_project(db_session, "alpha")
    beta = await _add_project(db_session, "beta")

    conv = Conversation(agent="beru_core", title="alpha talk", project_id=alpha.id)
    db_session.add(conv)
    await db_session.flush()
    for idx in range(2):
        db_session.add(Message(conversation_id=conv.id, role="user", content=f"n{idx}"))

    old_cutoff = datetime.now(timezone.utc) - timedelta(hours=50)
    other = Conversation(agent="beru_core", title="beta old", project_id=beta.id)
    db_session.add(other)
    await db_session.flush()
    stale_msg = Message(conversation_id=other.id, role="user", content="old")
    stale_msg.created_at = old_cutoff
    db_session.add(stale_msg)
    await db_session.commit()

    on_alpha = monitor.add_trigger(
        _threshold_trigger(
            "a", f"conversations.project_messages_24h:{alpha.id}", 2, operator="gte"
        )
    )
    on_beta = monitor.add_trigger(
        _threshold_trigger(
            "b", f"conversations.project_messages_24h:{beta.id}", 2, operator="gte"
        )
    )
    fired = await monitor.check_once()
    assert on_alpha.id in fired
    assert on_beta.id not in fired


# ---------------------------------------------------------------------------
# Cross-cutting behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_fire_writes_history_with_scoped_value(_proactive, db_session):
    """A scoped firing records the scoped value in the fire-history log."""
    _, monitor = _proactive
    await _add_fact(db_session, "w1", "x", category="work")
    await _add_fact(db_session, "w2", "y", category="work")
    await _add_fact(db_session, "w3", "z", category="work")
    await db_session.commit()

    trigger = monitor.add_trigger(
        _threshold_trigger("t", "memory.facts_count:work", 2, operator="gte")
    )
    fired = await monitor.check_once()
    assert trigger.id in fired

    rows = await list_trigger_fires(db_session, trigger.id)
    assert len(rows) == 1
    assert rows[0].condition["source"] == "memory.facts_count:work"
    assert rows[0].value == 3


@pytest.mark.asyncio
async def test_scoped_source_without_scope_stays_quiet(_proactive, db_session):
    """A bare scoped-only source reference is never fetched, so it never fires."""
    _, monitor = _proactive
    await _add_fact(db_session, "w1", "x", category="work")
    await db_session.commit()

    trigger = monitor.add_trigger(
        _threshold_trigger("t", "memory.facts_project_count", 0, operator="gte")
    )
    assert await monitor.check_once() == []
    assert trigger.fire_count == 0


@pytest.mark.asyncio
async def test_pushed_scoped_value_for_unregistered_source(_proactive):
    """Pushed values still drive sources that are not registered as scoped."""
    _, monitor = _proactive
    monitor.update_source_value("sensors.temperature:office", 88)

    trigger = monitor.add_trigger(
        _threshold_trigger("heat", "sensors.temperature:office", 80, operator="gt")
    )
    fired = await monitor.check_once()
    assert trigger.id in fired