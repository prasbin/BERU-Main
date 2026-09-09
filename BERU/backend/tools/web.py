"""Web search tool.

Allows agents to search the web for information. Real web search is not
implemented yet — no search provider is configured — so every call reports
``unavailable`` rather than fabricating search results.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    """Search the web for information on a topic."""

    name = "web_search"
    description = "Search the web for information on a given topic."
    permissions = ["read"]
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "num_results": {
                "type": "integer",
                "description": "Number of results to return (default 5).",
            },
        },
        "required": ["query"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure(
            "Tool 'web_search' is unavailable: no real web search provider is configured."
        )