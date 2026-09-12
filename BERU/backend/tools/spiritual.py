"""Spiritual tools for the DHANUS agent.

``lookup_scripture`` searches a **real, operator-supplied** public-domain
corpus under ``data/scripture/{tradition}.json`` — BERU never fabricates a
passage. When no corpus is installed for a tradition the tool says so, and when
no passage matches the topic it reports zero matches.

``meditation_timer`` starts a genuine timer: it records real UTC timestamps for
``now`` and the planned end (``now + duration``), persists the session, and
returns them so the caller can measure real elapsed time against the plan.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.tools.base import Tool, ToolResult
from backend.tools.stores import JsonStore, StoreError, default_data_dir

_SAFE_TRADITION = re.compile(r"[a-z0-9_\-]+")

_TECHNIQUES = ("breathing", "body_scan", "open_awareness", "loving_kindness")


def _safe_name(value: str) -> str:
    """Keep only a filesystem-safe identifier for tradition corpus names."""
    cleaned = value.strip().lower()
    cleaned = _SAFE_TRADITION.search(cleaned)
    return cleaned.group(0) if cleaned else ""


class LookupScriptureTool(Tool):
    """Look up a passage from a spiritual text or tradition."""

    name = "lookup_scripture"
    description = (
        "Look up a passage or teaching from a spiritual text, "
        "philosophical work, or wisdom tradition."
    )
    permissions = ["read"]
    availability = "available"  # genuine lookup over an installed corpus
    parameters = {
        "type": "object",
        "properties": {
            "tradition": {
                "type": "string",
                "description": "The spiritual tradition (e.g. 'vedantic', 'buddhist', 'stoic').",
            },
            "topic": {
                "type": "string",
                "description": "The topic or theme to look up.",
            },
        },
        "required": ["tradition", "topic"],
    }

    def __init__(self, corpus_dir: str | Path | None = None) -> None:
        self._corpus_dir = Path(
            corpus_dir if corpus_dir is not None else default_data_dir() / "scripture"
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        tradition = _safe_name(kwargs.get("tradition", ""))
        topic = kwargs.get("topic", "")
        if not tradition:
            return ToolResult.failure("lookup_scripture requires a 'tradition'.")
        if not topic or not topic.strip():
            return ToolResult.failure("lookup_scripture requires a non-empty 'topic'.")

        corpus_path = self._corpus_dir / f"{tradition}.json"
        if not corpus_path.is_file():
            return ToolResult.failure(
                f"No scripture corpus is installed for tradition '{tradition}' "
                f"(expected {corpus_path}). Install a public-domain corpus file "
                "in that location to enable real lookups."
            )
        try:
            with open(corpus_path, encoding="utf-8") as handle:
                corpus = json.load(handle)
        except (OSError, ValueError) as exc:
            return ToolResult.failure(
                f"Scripture corpus for '{tradition}' could not be read: {exc}"
            )

        passages = corpus.get("passages", [])
        if not isinstance(passages, list):
            return ToolResult.failure(
                f"Scripture corpus for '{tradition}' is malformed: "
                "'passages' must be a list."
            )

        needle = topic.strip().casefold()
        matches = []
        for passage in passages:
            haystack = (
                str(passage.get("text", ""))
                + " "
                + " ".join(str(k) for k in (passage.get("topics") or []))
            ).casefold()
            if needle in haystack:
                matches.append(
                    {
                        "reference": passage.get("reference", ""),
                        "text": passage.get("text", ""),
                        "source": passage.get("source", tradition),
                    }
                )

        return ToolResult.success(
            {
                "tradition": tradition,
                "topic": topic,
                "matches": matches,
                "match_count": len(matches),
                "passages_scanned": len(passages),
            }
        )


class MeditationTimerTool(Tool):
    """Guide a meditation session with timer settings."""

    name = "meditation_timer"
    description = "Set up a meditation timer with guidance settings and duration."
    permissions = ["read"]
    availability = "available"
    parameters = {
        "type": "object",
        "properties": {
            "duration_minutes": {
                "type": "integer",
                "description": "Meditation duration in minutes.",
            },
            "technique": {
                "type": "string",
                "description": (
                    "Meditation technique (e.g. 'breathing', 'body_scan', 'open_awareness')."
                ),
            },
        },
        "required": ["duration_minutes"],
    }

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._store = JsonStore(
            Path(store_path) if store_path else default_data_dir() / "meditation_sessions.json"
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        try:
            duration_minutes = int(kwargs.get("duration_minutes", 0))
        except (TypeError, ValueError):
            return ToolResult.failure("meditation_timer requires an integer 'duration_minutes'.")
        if duration_minutes <= 0:
            return ToolResult.failure("meditation_timer requires 'duration_minutes' > 0.")

        technique = kwargs.get("technique") or "breathing"
        if technique not in _TECHNIQUES:
            return ToolResult.failure(
                f"Unknown technique '{technique}'. "
                f"Use one of {', '.join(_TECHNIQUES)}."
            )

        try:
            document = self._store.load({"sessions": []})
        except StoreError as exc:
            return ToolResult.failure(str(exc))
        sessions = document.get("sessions", [])

        started_at = datetime.now(timezone.utc)
        ends_at = started_at + timedelta(minutes=duration_minutes)
        session = {
            "session_id": uuid.uuid4().hex[:12],
            "technique": technique,
            "duration_minutes": duration_minutes,
            "started_at": started_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "status": "running",
        }
        sessions.append(session)
        try:
            self._store.save({"sessions": sessions})
        except (StoreError, OSError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            {
                "session_id": session["session_id"],
                "technique": technique,
                "duration_minutes": duration_minutes,
                "started_at": session["started_at"],
                "ends_at": session["ends_at"],
                "status": "running",
            }
        )