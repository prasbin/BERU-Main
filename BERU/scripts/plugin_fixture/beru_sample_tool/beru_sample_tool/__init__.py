"""Sample third-party BERU tool plugin.

Used by ``scripts/verify_plugin_install.py`` to prove the Stage 5.3 gate: a
tool shipped as its own wheel, installed into a clean environment, is
discovered through the ``beru.tools`` entry-point group and executes.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class GreetingTool(Tool):
    name = "greeting"
    description = "Return a friendly greeting."
    parameters = {
        "type": "object",
        "properties": {"who": {"type": "string", "description": "Who to greet."}},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        who = kwargs.get("who") or "world"
        return ToolResult.success({"greeting": f"Hello, {who}!"})