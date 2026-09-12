"""Study tools for the IGRIS agent.

Both tools are backed by durable JSON stores (:mod:`backend.tools.stores`)
under the project ``data/`` directory. Search is a real, case-insensitive
keyword pass over the stored note text/tags — it never fabricates results.
Flashcards both describe **and** persist: created cards carry a real stored
id and survive restarts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.tools.base import Tool, ToolResult
from backend.tools.stores import JsonStore, StoreError, default_data_dir


class SearchKnowledgeTool(Tool):
    """Search through stored study notes and knowledge base."""

    name = "search_knowledge"
    description = "Search stored study notes and knowledge base for relevant information."
    permissions = ["read"]
    availability = "available"
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

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._store = JsonStore(
            Path(store_path) if store_path else default_data_dir() / "knowledge.json"
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return ToolResult.failure("search_knowledge requires a non-empty 'query'.")

        try:
            document = self._store.load({"notes": []})
        except StoreError as exc:
            return ToolResult.failure(str(exc))
        notes = document.get("notes", [])

        needle = query.casefold()
        matches = [
            note
            for note in notes
            if needle in str(note.get("text", "")).casefold()
            or any(needle in str(tag).casefold() for tag in note.get("tags", []))
        ]
        return ToolResult.success(
            {
                "query": query,
                "matches": matches,
                "match_count": len(matches),
                "note": (
                    "Keyword search over the local knowledge store "
                    f"({len(notes)} notes indexed)."
                ),
            }
        )


class CreateFlashcardTool(Tool):
    """Create a flashcard for spaced repetition learning."""

    name = "create_flashcard"
    description = "Create a study flashcard with a question and answer for spaced repetition."
    permissions = ["write"]
    availability = "available"
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

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._store = JsonStore(
            Path(store_path) if store_path else default_data_dir() / "flashcards.json"
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        front = kwargs.get("front", "")
        back = kwargs.get("back", "")
        tags = kwargs.get("tags") or []
        if not front or not front.strip():
            return ToolResult.failure("create_flashcard requires a non-empty 'front'.")
        if not back or not back.strip():
            return ToolResult.failure("create_flashcard requires a non-empty 'back'.")

        try:
            document = self._store.load({"cards": []})
        except StoreError as exc:
            return ToolResult.failure(str(exc))
        cards = document.get("cards", [])

        card = {
            "id": uuid.uuid4().hex[:12],
            "front": front.strip(),
            "back": back.strip(),
            "tags": [str(tag) for tag in tags if str(tag).strip()],
            "status": "new",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        cards.append(card)
        try:
            self._store.save({"cards": cards})
        except (StoreError, OSError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            {"card": card, "flashcard_created": True, "card_count": len(cards)}
        )