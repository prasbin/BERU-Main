"""Calendar and tasks tool.

Backed by a durable local JSON store (:mod:`backend.tools.stores`) under the
project ``data/`` directory. Events and tasks genuinely persist across runs —
creating an event or completing a task changes real stored state, and listing
returns the real records rather than fabricated items.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.tools.base import Tool, ToolResult
from backend.tools.stores import JsonStore, StoreError, default_data_dir

_ACTIONS = ("create_event", "create_task", "list_events", "complete_task")


class CalendarTool(Tool):
    """Manage calendar events and tasks (create, list, complete)."""

    name = "calendar"
    description = "Manage calendar events and tasks (create, list, complete)."
    permissions = ["read", "write"]
    availability = "available"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Action to perform: 'create_event', 'create_task', "
                    "'list_events', 'complete_task'."
                ),
            },
            "title": {
                "type": "string",
                "description": "Title of the event or task.",
            },
            "date": {
                "type": "string",
                "description": "Date for the event/task (ISO 8601 format).",
            },
            "kind": {
                "type": "string",
                "description": "Optional filter for 'list_events': 'event' or 'task'.",
            },
            "task_id": {
                "type": "string",
                "description": "ID of a task to mark as complete.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._store = JsonStore(
            Path(store_path) if store_path else default_data_dir() / "calendar.json"
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "")
        if action not in _ACTIONS:
            return ToolResult.failure(
                f"Unknown calendar action '{action}'. "
                f"Use one of {', '.join(_ACTIONS)}."
            )

        try:
            document = self._store.load({"items": []})
        except StoreError as exc:
            return ToolResult.failure(str(exc))
        items: list[dict] = document.get("items", [])

        if action == "list_events":
            return self._list(items, kwargs.get("kind") or "")

        try:
            if action == "create_event":
                item = self._create(kind="event", status="scheduled", **kwargs)
                items.append(item)
            elif action == "create_task":
                item = self._create(kind="task", status="open", **kwargs)
                items.append(item)
            else:  # complete_task
                item = self._complete(items, kwargs.get("task_id", ""))
        except (StoreError, OSError) as exc:
            return ToolResult.failure(str(exc))

        try:
            self._store.save({"items": items})
        except (StoreError, OSError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(dict(item))

    @staticmethod
    def _create(*, kind: str, status: str, **kwargs: Any) -> dict:
        title = kwargs.get("title", "")
        date = kwargs.get("date", "")
        if not title or not title.strip():
            raise StoreError(f"calendar {kind} requires a non-empty 'title'.")
        return {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "title": title.strip(),
            "date": date,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _list(items: list[dict], kind: str) -> ToolResult:
        if kind not in ("", "event", "task"):
            return ToolResult.failure(
                "Invalid 'kind' filter for list_events: use 'event' or 'task'."
            )
        visible = [item for item in items if not kind or item.get("kind") == kind]
        return ToolResult.success({"items": visible, "count": len(visible)})

    @staticmethod
    def _complete(items: list[dict], task_id: str) -> dict:
        if not task_id:
            raise StoreError("complete_task requires a 'task_id'.")
        for item in items:
            if item.get("id") == task_id:
                if item.get("kind") != "task":
                    raise StoreError(
                        f"Item '{task_id}' is a {item.get('kind')}, not a task."
                    )
                item["status"] = "done"
                return item
        raise StoreError(f"No task with id '{task_id}' was found.")