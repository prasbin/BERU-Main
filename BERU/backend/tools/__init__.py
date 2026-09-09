"""Tool subsystem.

Tools are modular, discoverable capabilities with explicit permissions. The
foundation defines the interface and registry plus one safe example tool. Tools
are NOT auto-executed by agents yet — wiring tool use into the reasoning loop
(with confirmation for sensitive actions) is a later, deliberate step.
"""

from backend.tools.base import Tool, ToolResult
from backend.tools.registry import ToolRegistry, get_tool_registry

__all__ = ["Tool", "ToolResult", "ToolRegistry", "get_tool_registry"]
