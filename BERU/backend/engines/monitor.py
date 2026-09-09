"""Event monitor and triggers — watches for conditions and fires actions.

Monitors can poll data sources, watch for threshold breaches, or respond to
system events. When a trigger condition is met, associated actions are executed
(notifications, agent tasks, scheduled jobs).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from backend.engines.notifications import Notification, NotificationLevel, NotificationService

logger = logging.getLogger(__name__)


class TriggerStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    FIRED = "fired"
    DISABLED = "disabled"


class ConditionType(str, Enum):
    THRESHOLD = "threshold"
    PATTERN = "pattern"
    CHANGED = "changed"
    CUSTOM = "custom"


@dataclass
class TriggerCondition:
    """A condition that, when true, fires the trigger."""
    condition_type: ConditionType = ConditionType.CUSTOM
    source: str = ""
    operator: str = "eq"
    value: Any = None
    params: dict[str, Any] = field(default_factory=dict)

    def evaluate(self, current_value: Any) -> bool:
        """Evaluate if the condition is met."""
        if self.condition_type == ConditionType.THRESHOLD:
            return self._eval_threshold(current_value)
        elif self.condition_type == ConditionType.PATTERN:
            return self._eval_pattern(current_value)
        elif self.condition_type == ConditionType.CHANGED:
            return current_value != self.value
        return bool(current_value)

    def _eval_threshold(self, current_value: Any) -> bool:
        try:
            val = float(current_value) if current_value is not None else 0
            threshold = float(self.value) if self.value is not None else 0
            if self.operator == "gt":
                return val > threshold
            elif self.operator == "gte":
                return val >= threshold
            elif self.operator == "lt":
                return val < threshold
            elif self.operator == "lte":
                return val <= threshold
            elif self.operator == "eq":
                return val == threshold
            elif self.operator == "neq":
                return val != threshold
        except (TypeError, ValueError):
            return False
        return False

    def _eval_pattern(self, current_value: Any) -> bool:
        if not isinstance(current_value, str):
            return False
        pattern = str(self.value) if self.value else ""
        operator = self.params.get("match", "contains")
        if operator == "contains":
            return pattern in current_value
        elif operator == "starts_with":
            return current_value.startswith(pattern)
        elif operator == "ends_with":
            return current_value.endswith(pattern)
        elif operator == "exact":
            return current_value == pattern
        return False


@dataclass
class Trigger:
    """A trigger that monitors a condition and fires actions."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    description: str = ""
    status: TriggerStatus = TriggerStatus.ACTIVE
    condition: TriggerCondition = field(default_factory=TriggerCondition)
    actions: list[dict[str, Any]] = field(default_factory=list)
    last_fired: datetime | None = None
    fire_count: int = 0
    cooldown_seconds: float = 0
    # Per-source audit retention policy (None = use the global default).
    retention_days: float | None = None
    keep_last: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "condition": {
                "condition_type": self.condition.condition_type.value,
                "source": self.condition.source,
                "operator": self.condition.operator,
                "value": self.condition.value,
                "params": self.condition.params,
            },
            "actions": self.actions,
            "last_fired": self.last_fired.isoformat() if self.last_fired else None,
            "fire_count": self.fire_count,
            "cooldown_seconds": self.cooldown_seconds,
            "retention_days": self.retention_days,
            "keep_last": self.keep_last,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trigger:
        cond_data = data.get("condition", {})
        created_at = data.get("created_at")
        if created_at and isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        last_fired = data.get("last_fired")
        if last_fired and isinstance(last_fired, str):
            last_fired = datetime.fromisoformat(last_fired)

        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            name=data.get("name", ""),
            description=data.get("description", ""),
            status=TriggerStatus(data.get("status", "active")),
            condition=TriggerCondition(
                condition_type=ConditionType(cond_data.get("condition_type", "custom")),
                source=cond_data.get("source", ""),
                operator=cond_data.get("operator", "eq"),
                value=cond_data.get("value"),
                params=cond_data.get("params", {}),
            ),
            actions=data.get("actions", []),
            last_fired=last_fired,
            fire_count=data.get("fire_count", 0),
            cooldown_seconds=data.get("cooldown_seconds", 0),
            retention_days=data.get("retention_days"),
            keep_last=data.get("keep_last"),
            created_at=created_at or datetime.now(timezone.utc),
        )


class EventMonitor:
    """Monitors data sources and fires triggers when conditions are met."""

    def __init__(self, notification_service: NotificationService) -> None:
        self._notifications = notification_service
        self._triggers: dict[str, Trigger] = {}
        self._sources: dict[str, Callable[..., Awaitable[Any]]] = {}
        # Parameterised sources: fetcher(scope) -> value. Triggers reference
        # them as "<base_name>:<scope>" (e.g. "memory.facts_count:work").
        self._scoped_sources: dict[str, Callable[[str], Awaitable[Any]]] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._current_values: dict[str, Any] = {}
        # Optional async hooks: persist/broadcast a fired notification and run
        # a trigger's action list (agent tasks, etc.).
        self._sink: Callable[[Notification], Awaitable[None]] | None = None
        self._action_runner: Callable[[Trigger], Awaitable[None]] | None = None
        # Optional async hook called with a trigger after it fires, so state
        # changes (status/fire_count/last_fired) can be persisted.
        self._state_hook: Callable[[Trigger], Awaitable[None]] | None = None
        # Optional async hook called after a trigger fires with the trigger and
        # the source value that satisfied its condition. Used for fire history.
        self._fire_hook: Callable[[Trigger, Any], Awaitable[None]] | None = None

    def set_sink(self, sink: Callable[[Notification], Awaitable[None]]) -> None:
        """Install an async hook called with each fired notification."""
        self._sink = sink

    def set_action_runner(self, runner: Callable[[Trigger], Awaitable[None]]) -> None:
        """Install an async hook that executes a trigger's ``actions``."""
        self._action_runner = runner

    def set_state_hook(self, hook: Callable[[Trigger], Awaitable[None]]) -> None:
        """Install an async hook invoked after a trigger fires.

        The hook receives the fired trigger snapshot (status, fire count, last
        fired time) so persistent state can be updated. Optional — the monitor
        works standalone without one.
        """
        self._state_hook = hook

    async def _save_state(self, trigger: Trigger) -> None:
        if self._state_hook is not None:
            await self._state_hook(trigger)

    def set_fire_hook(
        self, hook: Callable[[Trigger, Any], Awaitable[None]]
    ) -> None:
        """Install an async hook invoked after a trigger fires.

        The hook receives the fired trigger and the source value that satisfied
        its condition, so a snapshot can be appended to the fire-history log.
        Optional — the monitor works standalone without one.
        """
        self._fire_hook = hook

    async def _record_fire(self, trigger: Trigger, value: Any) -> None:
        if self._fire_hook is not None:
            await self._fire_hook(trigger, value)

    def list_sources(self) -> list[str]:
        """Return the names of all registered data sources."""
        return list(self._sources.keys()) + list(self._scoped_sources.keys())

    @property
    def running(self) -> bool:
        """Whether the background monitor loop is currently running."""
        return self._running

    def register_source(
        self, name: str, fetcher: Callable[..., Awaitable[Any]]
    ) -> None:
        """Register a data source that can be monitored."""
        self._sources[name] = fetcher

    def register_scoped_source(
        self, name: str, fetcher: Callable[[str], Awaitable[Any]]
    ) -> None:
        """Register a parameterised data source.

        Triggers reference it as ``"<name>:<scope>"``; each check fetches the
        value with the trigger's scope argument, e.g. ``"memory.facts_count:work"``
        counts facts in the ``work`` category. The bare name is never fetched —
        a trigger must supply a scope.
        """
        self._scoped_sources[name] = fetcher

    @staticmethod
    def _resolve_source(source: str) -> tuple[str, str | None]:
        """Split a trigger's source reference into ``(base_name, scope | None)``."""
        if source and ":" in source:
            base, _, scope = source.partition(":")
            return base, scope
        return source, None

    def add_trigger(self, trigger: Trigger) -> Trigger:
        """Add a trigger to monitor."""
        self._triggers[trigger.id] = trigger
        return trigger

    def get_trigger(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def list_triggers(self) -> list[Trigger]:
        return list(self._triggers.values())

    def pause_trigger(self, trigger_id: str) -> Trigger | None:
        trigger = self._triggers.get(trigger_id)
        if trigger:
            trigger.status = TriggerStatus.PAUSED
            return trigger
        return None

    def resume_trigger(self, trigger_id: str) -> Trigger | None:
        trigger = self._triggers.get(trigger_id)
        if trigger:
            trigger.status = TriggerStatus.ACTIVE
            return trigger
        return None

    def disable_trigger(self, trigger_id: str) -> Trigger | None:
        trigger = self._triggers.get(trigger_id)
        if trigger:
            trigger.status = TriggerStatus.DISABLED
            return trigger
        return None

    def delete_trigger(self, trigger_id: str) -> bool:
        if trigger_id in self._triggers:
            del self._triggers[trigger_id]
            return True
        return False

    def clear(self) -> int:
        """Remove every trigger and current source value. Returns count removed."""
        count = len(self._triggers)
        self._triggers.clear()
        self._current_values.clear()
        return count

    def update_source_value(self, source: str, value: Any) -> None:
        """Update the current value of a data source."""
        self._current_values[source] = value

    async def _check_triggers(self) -> None:
        """Check all active triggers against current values."""
        now = datetime.now(timezone.utc)

        for trigger in list(self._triggers.values()):
            if trigger.status != TriggerStatus.ACTIVE:
                continue

            source = trigger.condition.source
            current_value = self._current_values.get(source)

            if current_value is None:
                continue

            # Check cooldown
            if trigger.last_fired and trigger.cooldown_seconds > 0:
                elapsed = (now - trigger.last_fired).total_seconds()
                if elapsed < trigger.cooldown_seconds:
                    continue
                # Cooldown elapsed — re-arm so the trigger can fire again.
                trigger.status = TriggerStatus.ACTIVE

            if trigger.condition.evaluate(current_value):
                trigger.status = TriggerStatus.FIRED
                trigger.last_fired = now
                trigger.fire_count += 1

                # Send notification
                notification = self._notifications.notify(
                    title=f"Trigger fired: {trigger.name}",
                    message=(
                        f"Condition met on source '{source}': "
                        f"{trigger.condition.operator} {trigger.condition.value}"
                    ),
                    level=NotificationLevel.WARNING,
                )

                # Persist/broadcast the alert through the external sink.
                if self._sink is not None:
                    try:
                        await self._sink(notification)
                    except Exception:
                        logger.exception("Notification sink failed for trigger '%s'", trigger.name)

                # Re-arm after firing
                if trigger.cooldown_seconds == 0:
                    trigger.status = TriggerStatus.ACTIVE

                # Execute the trigger's configured actions (agent tasks, etc.).
                if trigger.actions and self._action_runner is not None:
                    try:
                        await self._action_runner(trigger)
                    except Exception:
                        logger.exception("Action runner failed for trigger '%s'", trigger.name)

                logger.info(
                    "Trigger '%s' fired (count: %d)", trigger.name, trigger.fire_count
                )

    async def check_once(self) -> list[str]:
        """Refresh registered sources, then evaluate all active triggers.

        Returns the ids of any triggers that fired on this pass. Exposed for
        deterministic manual ticks (API + tests).
        """
        # Fetch current values from sources
        for name, fetcher in self._sources.items():
            try:
                value = await fetcher()
                self._current_values[name] = value
            except Exception:
                logger.exception("Failed to fetch source '%s'", name)

        # Fetch parameterised sources referenced (with a scope) by active
        # triggers. Pushed values for unregistered/live-parameterised sources
        # are left untouched so ``update_source_value`` still drives them.
        scoped_fetched: dict[str, Any] = {}
        for trigger in self._triggers.values():
            if trigger.status != TriggerStatus.ACTIVE:
                continue
            base, scope = self._resolve_source(trigger.condition.source)
            if scope is None or base not in self._scoped_sources:
                continue
            key = trigger.condition.source
            if key in scoped_fetched:
                continue
            try:
                scoped_fetched[key] = await self._scoped_sources[base](scope)
            except Exception:
                logger.exception("Failed to fetch scoped source '%s'", key)
        self._current_values.update(scoped_fetched)

        fired_before = {t.id: t.fire_count for t in self._triggers.values()}
        await self._check_triggers()

        fired_ids: list[str] = []
        for trigger in self._triggers.values():
            if trigger.fire_count > fired_before.get(trigger.id, 0):
                fired_ids.append(trigger.id)
                # Persist the fired snapshot (status, count, last fired time).
                await self._save_state(trigger)
                # Append fire history with the value that satisfied the condition.
                await self._record_fire(
                    trigger, self._current_values.get(trigger.condition.source)
                )
        return fired_ids

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            self._wake.clear()
            try:
                await self.check_once()
            except Exception:
                logger.exception("Monitor loop error")

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        """Start the monitoring loop."""
        if self._running:
            return
        self._running = True
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("Event monitor started")

    async def stop(self) -> None:
        """Stop the monitoring loop gracefully (no work cancelled mid-session)."""
        self._running = False
        if self._wake:
            self._wake.set()
        if self._task:
            await self._task
            self._task = None
        logger.info("Event monitor stopped")
