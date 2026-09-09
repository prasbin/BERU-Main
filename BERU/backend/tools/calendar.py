"""Calendar and tasks tool.

Allows agents to manage calendar events and tasks. Stub implementation returns
structured results ready to be connected to a real calendar/task backend.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class CalendarTool(Tool):
    """Manage calendar events and tasks."""

    name = "calendar"
    description = "Manage calendar events and tasks (create, list, complete)."
    permissions = ["read", "write"]
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Action to perform: 'create_event', 'list_events', 'complete_task'.",
            },
            "title": {
                "type": "string",
                "description": "Title of the event or task.",
            },
            "date": {
                "type": "string",
                "description": "Date for the event/task (ISO 8601 format).",
            },
            "task_id": {
                "type": "string",
                "description": "ID of a task to mark as complete.",
            },
        },
        "required": ["action"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "")
        title = kwargs.get("title", "")
        date = kwargs.get("date", "")
        task_id = kwargs.get("task_id", "")

        if action == "create_event":
            return ToolResult.success(
                {
                    "action": action,
                    "event_created": True,
                    "title": title,
                    "date": date,
                    "event_id": "evt_stub_1",
                }
            )
        elif action == "list_events":
            return ToolResult.success(
                {
                    "action": action,
                    "events": [
                        {
                            "id": "evt_stub_1",
                            "title": "Stub event",
                            "date": "2025-01-01T10:00:00Z",
                        }
                    ],
                    "count": 1,
                }
            )
        elif action == "complete_task":
            return ToolResult.success(
                {
                    "action": action,
                    "task_id": task_id,
                    "completed": True,
                    "message": f"Task '{task_id}' marked as complete (stub).",
                }
            )
        else:
            return ToolResult.failure(
                f"Unknown calendar action '{action}'. "
                "Use 'create_event', 'list_events', or 'complete_task'."
            )
