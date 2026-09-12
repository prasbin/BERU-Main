"""Calendar and tasks tool.

Allows agents to manage calendar events and tasks. The real calendar/task
backend is not implemented yet, so the tool reports itself as unavailable
instead of fabricating events or task state.
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
        if action not in ("create_event", "list_events", "complete_task"):
            return ToolResult.failure(
                f"Unknown calendar action '{action}'. "
                "Use 'create_event', 'list_events', or 'complete_task'."
            )
        return ToolResult.failure(
            f"calendar action '{action}' is unavailable: the calendar/task "
            "backend is not implemented, so no event or task state can be "
            "read or changed."
        )
