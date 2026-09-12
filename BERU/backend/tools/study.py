"""Study tools for the IGRIS agent.

Tools to assist with academic research, note-taking, and learning.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class SearchKnowledgeTool(Tool):
    """Search through stored study notes and knowledge base."""

    name = "search_knowledge"
    description = "Search stored study notes and knowledge base for relevant information."
    permissions = ["read"]
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to find relevant study material.",
            }
        },
        "required": ["query"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure(
            "search_knowledge is unavailable: no study knowledge base backend "
            "is configured, so no stored notes can be searched yet."
        )


class CreateFlashcardTool(Tool):
    """Create a flashcard for spaced repetition learning."""

    name = "create_flashcard"
    description = "Create a study flashcard with a question and answer for spaced repetition."
    permissions = ["write"]
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "front": {
                "type": "string",
                "description": "The question or prompt side of the flashcard.",
            },
            "back": {
                "type": "string",
                "description": "The answer or explanation side of the flashcard.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for categorisation.",
            },
        },
        "required": ["front", "back"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure(
            "create_flashcard is unavailable: the flashcard storage backend is "
            "not implemented, so no flashcard can be created yet."
        )
