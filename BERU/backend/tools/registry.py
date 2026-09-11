"""Tool registry: registration and discovery of available tools.

The registry is seeded with BERU's built-in tools and then extended with any
selected third-party tools advertised through the ``beru.tools`` entry-point
group (see :mod:`backend.plugins.discovery`). Built-in names always win.
"""

from __future__ import annotations

from functools import lru_cache

from backend.core.errors import NotFoundError
from backend.core.logging import get_logger
from backend.plugins import discovery
from backend.tools.base import Tool
from backend.tools.clock import ClockTool

logger = get_logger(__name__)


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


def _register_plugins(registry: ToolRegistry) -> None:
    """Register plugin tools advertised via the ``beru.tools`` entry points."""
    for name, factory in discovery.load_entry_point_factories(
        discovery.GROUP_TOOLS
    ).items():
        try:
            tool = factory()
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not block startup
            logger.warning("Skipping plugin tool '%s' (factory failed): %s", name, exc)
            continue
        if not isinstance(tool, Tool):
            logger.warning(
                "Skipping plugin tool '%s': entry point did not yield a Tool (got %s).",
                name,
                type(tool).__name__,
            )
            continue
        try:
            registry.register(tool)
        except ValueError:
            logger.warning(
                "Skipping plugin tool '%s': a tool named '%s' is already registered.",
                name,
                tool.name,
            )
    return None


@lru_cache
def get_tool_registry() -> ToolRegistry:
    """Return the process-wide tool registry, seeded with built-in tools."""
    registry = ToolRegistry()
    registry.register(ClockTool())
    _register_plugins(registry)
    return registry