"""Tool registry: registration and discovery of available tools."""

from __future__ import annotations

from functools import lru_cache

from backend.core.errors import NotFoundError
from backend.tools.base import Tool
from backend.tools.clock import ClockTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise NotFoundError(
                f"Unknown tool '{name}'.", detail={"available": sorted(self._tools)}
            )
        return tool

    def list(self) -> list[Tool]:
        return list(self._tools.values())


@lru_cache
def get_tool_registry() -> ToolRegistry:
    """Return the process-wide tool registry, seeded with built-in tools."""
    registry = ToolRegistry()
    registry.register(ClockTool())
    return registry
