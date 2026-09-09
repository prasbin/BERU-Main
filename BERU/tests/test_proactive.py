"""Tests for the proactive/autonomous system: scheduler, notifications, monitor."""

from __future__ import annotations

from backend.engines.monitor import (
    ConditionType,
    EventMonitor,
    Trigger,
    TriggerCondition,
    TriggerStatus,
)
from backend.engines.notifications import Notification, NotificationLevel, NotificationService
from backend.engines.scheduler import (
    ScheduledTask,
    Scheduler,
    TaskStatus,
    TaskType,
)

# ---- Scheduler tests ----


def test_scheduler_add_task():
    scheduler = Scheduler()
    task = ScheduledTask(name="test_task", task_type=TaskType.INTERVAL, interval_seconds=60)
    scheduler.add_task(task)
    assert len(scheduler.list_tasks()) == 1


def test_scheduler_get_task():
    scheduler = Scheduler()
    task = ScheduledTask(name="test_task")
    scheduler.add_task(task)
    retrieved = scheduler.get_task(task.id)
    assert retrieved is not None
    assert retrieved.name == "test_task"


def test_scheduler_pause_resume():
    scheduler = Scheduler()
    task = ScheduledTask(name="test_task", task_type=TaskType.INTERVAL, interval_seconds=60)
    scheduler.add_task(task)

    scheduler.pause_task(task.id)
    assert scheduler.get_task(task.id).status == TaskStatus.PAUSED

    scheduler.resume_task(task.id)
    assert scheduler.get_task(task.id).status == TaskStatus.PENDING


def test_scheduler_cancel():
    scheduler = Scheduler()
    task = ScheduledTask(name="test_task")
    scheduler.add_task(task)
    scheduler.cancel_task(task.id)
    assert scheduler.get_task(task.id).status == TaskStatus.CANCELLED


def test_scheduler_delete():
    scheduler = Scheduler()
    task = ScheduledTask(name="test_task")
    scheduler.add_task(task)
    assert scheduler.delete_task(task.id) is True
    assert scheduler.get_task(task.id) is None
    assert scheduler.delete_task("nonexistent") is False


def test_scheduler_list_active():
    scheduler = Scheduler()
    t1 = ScheduledTask(name="active", task_type=TaskType.INTERVAL, interval_seconds=60)
    t2 = ScheduledTask(name="paused")
    scheduler.add_task(t1)
    scheduler.add_task(t2)
    scheduler.pause_task(t2.id)

    active = scheduler.list_active_tasks()
    assert len(active) == 1
    assert active[0].name == "active"


def test_task_serialization():
    task = ScheduledTask(
        name="test",
        task_type=TaskType.INTERVAL,
        interval_seconds=30,
        payload={"key": "value"},
    )
    data = task.to_dict()
    restored = ScheduledTask.from_dict(data)
    assert restored.name == "test"
    assert restored.interval_seconds == 30
    assert restored.payload == {"key": "value"}


# ---- Notification tests ----


def test_notification_create():
    service = NotificationService()
    notif = service.notify("Test", "Test message", NotificationLevel.INFO)
    assert notif.title == "Test"
    assert notif.read is False


def test_notification_list():
    service = NotificationService()
    service.notify("Alert 1", "Message 1")
    service.notify("Alert 2", "Message 2")
    assert len(service.list()) == 2


def test_notification_unread_count():
    service = NotificationService()
    service.notify("Alert 1", "Message 1")
    service.notify("Alert 2", "Message 2")
    assert service.unread_count == 2

    notifs = service.list()
    service.mark_read(notifs[0].id)
    assert service.unread_count == 1


def test_notification_mark_all_read():
    service = NotificationService()
    service.notify("Alert 1", "Message 1")
    service.notify("Alert 2", "Message 2")
    count = service.mark_all_read()
    assert count == 2
    assert service.unread_count == 0


def test_notification_delete():
    service = NotificationService()
    notif = service.notify("Test", "Message")
    assert service.delete(notif.id) is True
    assert service.delete("nonexistent") is False


def test_notification_clear():
    service = NotificationService()
    service.notify("Alert 1", "Message 1")
    service.notify("Alert 2", "Message 2")
    count = service.clear()
    assert count == 2
    assert len(service.list()) == 0


def test_notification_filter_unread():
    service = NotificationService()
    n1 = service.notify("Alert 1", "Message 1")
    service.notify("Alert 2", "Message 2")
    service.mark_read(n1.id)

    unread = service.list(unread_only=True)
    assert len(unread) == 1
    assert unread[0].title == "Alert 2"


def test_notification_serialization():
    notif = Notification(title="Test", message="Msg", level=NotificationLevel.WARNING)
    data = notif.to_dict()
    restored = Notification.from_dict(data)
    assert restored.title == "Test"
    assert restored.level == NotificationLevel.WARNING


# ---- Trigger/Monitor tests ----


def test_trigger_condition_threshold():
    cond = TriggerCondition(
        condition_type=ConditionType.THRESHOLD,
        operator="gt",
        value=100,
    )
    assert cond.evaluate(150) is True
    assert cond.evaluate(50) is False
    assert cond.evaluate(100) is False


def test_trigger_condition_pattern():
    cond = TriggerCondition(
        condition_type=ConditionType.PATTERN,
        value="error",
        params={"match": "contains"},
    )
    assert cond.evaluate("An error occurred") is True
    assert cond.evaluate("All good") is False


def test_trigger_condition_changed():
    cond = TriggerCondition(
        condition_type=ConditionType.CHANGED,
        value="old_value",
    )
    assert cond.evaluate("new_value") is True
    assert cond.evaluate("old_value") is False


def test_trigger_serialization():
    trigger = Trigger(
        name="test_trigger",
        condition=TriggerCondition(
            condition_type=ConditionType.THRESHOLD,
            source="cpu_usage",
            operator="gt",
            value=90,
        ),
    )
    data = trigger.to_dict()
    restored = Trigger.from_dict(data)
    assert restored.name == "test_trigger"
    assert restored.condition.source == "cpu_usage"
    assert restored.condition.value == 90


async def test_monitor_add_trigger():
    service = NotificationService()
    monitor = EventMonitor(service)

    trigger = Trigger(
        name="test",
        condition=TriggerCondition(
            condition_type=ConditionType.THRESHOLD,
            source="test_source",
            operator="gt",
            value=50,
        ),
    )
    monitor.add_trigger(trigger)
    assert len(monitor.list_triggers()) == 1


async def test_monitor_pause_resume():
    service = NotificationService()
    monitor = EventMonitor(service)

    trigger = Trigger(name="test")
    monitor.add_trigger(trigger)

    monitor.pause_trigger(trigger.id)
    assert monitor.get_trigger(trigger.id).status == TriggerStatus.PAUSED

    monitor.resume_trigger(trigger.id)
    assert monitor.get_trigger(trigger.id).status == TriggerStatus.ACTIVE


async def test_monitor_delete():
    service = NotificationService()
    monitor = EventMonitor(service)

    trigger = Trigger(name="test")
    monitor.add_trigger(trigger)
    assert monitor.delete_trigger(trigger.id) is True
    assert monitor.delete_trigger("nonexistent") is False
