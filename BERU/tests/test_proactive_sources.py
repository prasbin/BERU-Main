"""Tests for the richer monitor data sources (facts, conversation activity).

Covers the two new families of pluggable sources wired into the shared monitor:
``memory.facts_count`` / ``memory.facts_text`` (long-term fact memory) and
``conversations.new_24h`` / ``conversations.messages_24h`` (recent conversation
activity). Behaviour is asserted through trigger firing, exactly as the runtime
consumes them.
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


@pytest_asyncio.fixture
async def _proactive(_sessionmaker):
    """Point the shared proactive runtime at the temp DB and clear its state."""
    set_session_factory(lambda: _sessionmaker)
    get_scheduler().clear()
    get_event_monitor().clear()
    return get_scheduler(), get_event_monitor()


async def _add_fact(session, key: str, value: str, category: str = "general") -> None:
    await FactService().upsert(
        session, key=key, value=value, category=category, agent="beru_core"
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Source registration
# ---------------------------------------------------------------------------


def test_monitor_registers_rich_sources(_proactive):
    """The shared monitor exposes the fact and conversation-activity sources."""
    _, monitor = _proactive
    sources = set(monitor.list_sources())
    assert {
        "memory.facts_count",
        "memory.facts_text",
        "conversations.new_24h",
        "conversations.messages_24h",
    }.issubset(sources)


# ---------------------------------------------------------------------------
# Facts sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facts_count_fires_threshold_trigger(_proactive, db_session):
    """facts_count is the number of stored facts; a threshold trigger fires."""
    _, monitor = _proactive
    await _add_fact(db_session, "home_city", "Tokyo")
    await _add_fact(db_session, "prefers", "dark mode")
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="fact-overload",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="memory.facts_count",
                operator="gt",
                value=1,
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired


@pytest.mark.asyncio
async def test_facts_count_stays_below_threshold(_proactive, db_session):
    """A low fact count does not fire a high threshold."""
    _, monitor = _proactive
    await _add_fact(db_session, "home_city", "Tokyo")
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="fact-overload",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="memory.facts_count",
                operator="gt",
                value=5,
            ),
        )
    )
    assert await monitor.check_once() == []
    assert trigger.fire_count == 0


@pytest.mark.asyncio
async def test_facts_text_fires_pattern_trigger(_proactive, db_session):
    """facts_text exposes fact content as text; a pattern trigger can match it."""
    _, monitor = _proactive
    await _add_fact(db_session, "preferred_language", "Python")
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="python-fan",
            condition=TriggerCondition(
                condition_type=ConditionType.PATTERN,
                source="memory.facts_text",
                operator="contains",
                value="Python",
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired


@pytest.mark.asyncio
async def test_facts_text_no_match_does_not_fire(_proactive, db_session):
    _, monitor = _proactive
    await _add_fact(db_session, "preferred_language", "Python")
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="rust-fan",
            condition=TriggerCondition(
                condition_type=ConditionType.PATTERN,
                source="memory.facts_text",
                operator="contains",
                value="Rust",
            ),
        )
    )
    assert await monitor.check_once() == []
    assert trigger.fire_count == 0


# ---------------------------------------------------------------------------
# Conversation-activity sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversations_messages_24h_fires(_proactive, db_session):
    """Recent messages are counted; a threshold trigger fires."""
    _, monitor = _proactive
    conv = Conversation(agent="beru_core", title="today")
    db_session.add(conv)
    await db_session.flush()
    for idx in range(3):
        db_session.add(
            Message(conversation_id=conv.id, role="user", content=f"msg {idx}")
        )
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="busy",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="conversations.messages_24h",
                operator="gte",
                value=3,
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired


@pytest.mark.asyncio
async def test_conversations_new_24h_counts_only_recent(_proactive, db_session):
    """Old conversations/messages fall outside the 24h window and do not fire."""
    _, monitor = _proactive
    old_cutoff = datetime.now(timezone.utc) - timedelta(hours=50)

    stale = Conversation(agent="beru_core", title="stale")
    stale.created_at = old_cutoff
    stale.updated_at = old_cutoff
    db_session.add(stale)
    await db_session.flush()
    old_msg = Message(conversation_id=stale.id, role="user", content="old")
    old_msg.created_at = old_cutoff
    old_msg.updated_at = old_cutoff
    db_session.add(old_msg)
    await db_session.commit()

    fresh = Conversation(agent="beru_core", title="fresh")
    db_session.add(fresh)
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="any-activity",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="conversations.new_24h",
                operator="gte",
                value=1,
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired
    assert trigger.fire_count == 1

    # With no recent messages, a high message threshold stays quiet while the
    # new-24h trigger keeps firing (fresh conversation still counts).
    trigger2 = monitor.add_trigger(
        Trigger(
            name="message-volume",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="conversations.messages_24h",
                operator="gte",
                value=5,
            ),
        )
    )
    fired2 = await monitor.check_once()
    assert trigger.id in fired2
    assert trigger2.id not in fired2
    assert trigger.fire_count == 2
    assert trigger2.fire_count == 0


# ---------------------------------------------------------------------------
# Cross-project aggregate sources
# ---------------------------------------------------------------------------


async def _add_project(session, name: str) -> Project:
    project = Project(name=name)
    session.add(project)
    await session.flush()
    return project


async def _add_project_conversation(
    session,
    *,
    project_id: str | None = None,
    title: str = "conv",
    message_count: int = 0,
    age_hours: float | None = None,
):
    """Create a conversation with messages, optionally backdated and project-scoped."""
    conv = Conversation(agent="beru_core", title=title, project_id=project_id)
    session.add(conv)
    await session.flush()
    for idx in range(message_count):
        message = Message(conversation_id=conv.id, role="user", content=f"m{idx}")
        if age_hours is not None:
            old = datetime.now(timezone.utc) - timedelta(hours=age_hours)
            message.created_at = old
            message.updated_at = old
        session.add(message)
    await session.flush()
    return conv


def test_monitor_registers_project_aggregate_sources(_proactive):
    """The shared monitor exposes the cross-project aggregate sources."""
    _, monitor = _proactive
    sources = set(monitor.list_sources())
    assert {
        "projects.count",
        "projects.new_24h",
        "projects.messages_24h",
        "projects.active_24h",
    }.issubset(sources)


@pytest.mark.asyncio
async def test_projects_count_fires_threshold(_proactive, db_session):
    _, monitor = _proactive
    await _add_project(db_session, "alpha")
    await _add_project(db_session, "beta")
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="multi-project",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="projects.count",
                operator="gt",
                value=1,
            ),
        )
    )
    fired = await monitor.check_once()
    assert trigger.id in fired


@pytest.mark.asyncio
async def test_projects_new_24h_counts_only_recent(_proactive, db_session):
    _, monitor = _proactive
    await _add_project(db_session, "fresh")
    stale = await _add_project(db_session, "stale")
    stale.created_at = datetime.now(timezone.utc) - timedelta(hours=50)
    stale.updated_at = stale.created_at
    await db_session.commit()

    trigger = monitor.add_trigger(
        Trigger(
            name="project-growth",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="projects.new_24h",
                operator="gte",
                value=2,
            ),
        )
    )
    assert await monitor.check_once() == []
    assert trigger.fire_count == 0

    trigger2 = monitor.add_trigger(
        Trigger(
            name="project-growth-one",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="projects.new_24h",
                operator="gte",
                value=1,
            ),
        )
    )
    assert await monitor.check_once() == [trigger2.id]


@pytest.mark.asyncio
async def test_projects_messages_24h_excludes_unaffiliated(_proactive, db_session):
    """Only messages inside project conversations count towards the aggregate."""
    _, monitor = _proactive
    alpha = await _add_project(db_session, "alpha")
    await _add_project_conversation(
        db_session, project_id=alpha.id, title="alpha talk", message_count=2
    )
    await _add_project_conversation(
        db_session, title="no project", message_count=5
    )
    await db_session.commit()

    quiet = monitor.add_trigger(
        Trigger(
            name="project-volume",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="projects.messages_24h",
                operator="gte",
                value=3,
            ),
        )
    )
    assert await monitor.check_once() == []
    assert quiet.fire_count == 0

    modest = monitor.add_trigger(
        Trigger(
            name="project-volume-low",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="projects.messages_24h",
                operator="gte",
                value=2,
            ),
        )
    )
    assert await monitor.check_once() == [modest.id]


@pytest.mark.asyncio
async def test_projects_active_24h_counts_distinct_recent(_proactive, db_session):
    """active_24h is distinct projects with fresh activity; old volume is ignored."""
    _, monitor = _proactive
    alpha = await _add_project(db_session, "alpha")
    beta = await _add_project(db_session, "beta")
    gamma = await _add_project(db_session, "gamma")
    await _add_project_conversation(
        db_session, project_id=alpha.id, title="a", message_count=3
    )
    await _add_project_conversation(
        db_session, project_id=beta.id, title="b", message_count=1
    )
    await _add_project_conversation(
        db_session,
        project_id=gamma.id,
        title="g",
        message_count=2,
        age_hours=50,
    )
    await db_session.commit()

    # Projects alpha + beta are active in the window; gamma is not.
    expect_two = monitor.add_trigger(
        Trigger(
            name="active-two",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="projects.active_24h",
                operator="gte",
                value=2,
            ),
        )
    )
    fired = await monitor.check_once()
    assert expect_two.id in fired

    expect_three = monitor.add_trigger(
        Trigger(
            name="active-three",
            condition=TriggerCondition(
                condition_type=ConditionType.THRESHOLD,
                source="projects.active_24h",
                operator="gte",
                value=3,
            ),
        )
    )
    assert await monitor.check_once() == [expect_two.id]
    assert expect_three.fire_count == 0