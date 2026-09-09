"""Coding tools for the TANK agent.

Tools to assist with programming, code analysis, and software development.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class CodeAnalyzerTool(Tool):
    """Analyse code for quality, complexity, and potential issues."""

    name = "code_analyser"
    description = (
        "Analyse code for complexity, potential bugs, and improvement "
        "opportunities."
    )
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
        lines = code.strip().split("\n")
        return ToolResult.success(
            {
                "language": language,
                "line_count": len(lines),
                "complexity_estimate": "low" if len(lines) < 50 else "medium",
                "suggestions": [
                    "Consider adding docstrings for public functions.",
                    "Check for consistent error handling patterns.",
                ],
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
        code = kwargs.get("code", "")
        language = kwargs.get("language", "unknown")
        return ToolResult.success(
            {
                "formatted_code": code,
                "language": language,
                "message": "Code formatted (stub — connect to real formatter).",
            }
        )
