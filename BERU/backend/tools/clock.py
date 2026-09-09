"""Example tool: returns the current UTC time.

Deliberately trivial and side-effect-free — it exists to demonstrate and test
the tool interface, not to provide meaningful functionality.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.tools.base import Tool, ToolResult


class ClockTool(Tool):
    name = "clock"
    description = "Return the current date and time in UTC (ISO 8601)."
    permissions = ["read_clock"]
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success({"utc": datetime.now(timezone.utc).isoformat()})
