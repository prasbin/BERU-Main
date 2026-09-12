"""Coding tools for the TANK agent.

Tools to assist with programming, code analysis, and software development.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class CodeAnalyzerTool(Tool):
    """Analyse code for quality, complexity, and potential issues.

    The analysis is a local, purely heuristic pass (line counts and a crude
    size-based complexity estimate) computed directly from the supplied code.
    It never claims to perform semantic or AST-level analysis it cannot do.
    """

    name = "code_analyser"
    description = "Analyse code for complexity, potential bugs, and improvement opportunities."
    permissions = ["read"]
    availability = "limited"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The source code to analyse.",
            },
            "language": {
                "type": "string",
                "description": "Programming language (e.g. 'python', 'javascript').",
            },
        },
        "required": ["code"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        code = kwargs.get("code", "")
        language = kwargs.get("language", "unknown")
        lines = code.splitlines()
        nonblank = [ln for ln in lines if ln.strip()]
        return ToolResult.success(
            {
                "language": language,
                "line_count": len(lines),
                "nonblank_line_count": len(nonblank),
                "character_count": len(code),
                "complexity_estimate": "low" if len(lines) < 50 else "medium",
                "note": (
                    "Heuristic analysis only: metrics are computed from raw "
                    "line/character counts. No semantic or AST analysis is "
                    "performed."
                ),
            }
        )


class CodeFormatterTool(Tool):
    """Format code according to language conventions."""

    name = "code_formatter"
    description = "Format code according to language-specific style conventions."
    permissions = ["read"]
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The source code to format.",
            },
            "language": {
                "type": "string",
                "description": "Programming language (e.g. 'python', 'javascript').",
            },
        },
        "required": ["code"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure(
            "code_formatter is unavailable: no language formatter backend is "
            "implemented, so the supplied code is returned unmodified."
        )
