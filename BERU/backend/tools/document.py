"""File and document analysis tool.

Allows agents to read and analyse file content. The real file-I/O backend is
not implemented yet, so the tool reports itself as unavailable and never
fabricates analysis results.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class FileAnalyserTool(Tool):
    """Read and analyse a file's content."""

    name = "file_analyser"
    description = "Read and analyse the content of a file at a given path."
    permissions = ["read"]
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file path to analyse.",
            },
            "analysis_type": {
                "type": "string",
                "description": (
                    "Type of analysis: 'summary', 'stats', 'structure'."
                ),
            },
        },
        "required": ["path"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure(
            "file_analyser is unavailable: no file analysis backend is "
            "implemented, so no file can be read or analysed yet."
        )
