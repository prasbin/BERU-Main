"""Tool interface and result type.

Every tool declares a name, a human description, the permissions it requires,
and a JSON-schema-like parameter description (usable later for LLM function
calling). Tools return a structured :class:`ToolResult` rather than raising, so
callers can handle failure uniformly.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str | None = None

    @classmethod
    def success(cls, output: Any) -> ToolResult:
        return cls(ok=True, output=output)

    @classmethod
    def failure(cls, error: str) -> ToolResult:
        return cls(ok=False, error=error)

    @classmethod
    def permission_denied(cls, missing: list[str]) -> ToolResult:
        return cls(ok=False, error=f"Permission denied: missing {', '.join(missing)}")

    @classmethod
    def confirmation_required(cls, tool_name: str, reason: str = "") -> ToolResult:
        msg = f"Confirmation required for '{tool_name}'."
        if reason:
            msg += f" {reason}"
        return cls(ok=False, output={"confirmation_required": True}, error=msg)


#: Cap on the serialized tool result fed back to the model/ledger so a single
#: verbose tool can never balloon the context window.
_MAX_RESULT_CHARS = 12000


def _capped_result(rendered: str) -> str:
    if len(rendered) <= _MAX_RESULT_CHARS:
        return rendered
    total = len(rendered)
    return (
        '{"result": "<tool output truncated ("'
        + str(total)
        + ' chars total); run a more specific action to retrieve only the '
        'details you need.>"}'
    )


def serialize_tool_result(result: ToolResult, *, tool_name: str = "") -> str:
    """Render a :class:`ToolResult` as the JSON string fed back to the LLM.

    After a tool runs, the outcome is serialized into a fixed shape that the
    model consumes as a ``tool`` role message:

      * success      -> ``{"result": <output>}``
      * confirmation -> ``{"confirmation_required": true, "tool": ..., "message": ...}``
      * failure      -> ``{"error": <message>}``

    Oversized outputs are replaced with a short elision message (still valid
    JSON) so one verbose tool call cannot balloon the model context.
    """
    if result.ok:
        return _capped_result(json.dumps({"result": result.output}))
    if isinstance(result.output, dict) and result.output.get("confirmation_required"):
        return json.dumps(
            {
                "confirmation_required": True,
                "tool": tool_name,
                "message": f"Tool '{tool_name}' requires explicit user confirmation.",
            }
        )
    return _capped_result(json.dumps({"error": result.error}))


class Tool(ABC):
    #: Unique identifier.
    name: str = "tool"
    #: Human-readable description of what the tool does.
    description: str = ""
    #: Permission tags this tool requires (enforced by the caller/policy layer).
    permissions: list[str] = []
    #: JSON-schema-style description of accepted parameters (for future tool use).
    parameters: dict[str, Any] = {}
    #: If True, the tool requires explicit user confirmation before execution.
    requires_confirmation: bool = False
    #: Capability status for the live tool panel: "available" | "limited" |
    #: "unavailable". A stub/emulated tool must be "unavailable"; one that works
    #: but covers only a subset of its advertised behaviour is "limited".
    availability: str = "available"

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with validated keyword arguments."""
        raise NotImplementedError
