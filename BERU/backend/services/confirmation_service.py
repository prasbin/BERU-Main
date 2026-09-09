"""Pending tool-confirmation store.

When the agent requests a ``requires_confirmation`` tool, the intended tool call
is parked here until the user explicitly approves it. Confirmations are:

  * **single-use** — consumed once, so replaying an id cannot re-run a tool;
  * **conversation-bound** — an id only works for the conversation it was
    created for;
  * **transient** — held in memory and expire after ``ttl_seconds`` (matching
    the scheduler/notification services, which are also in-memory). A restart
    simply drops un-answered confirmations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from backend.core.errors import NotFoundError

DEFAULT_CONFIRMATION_TTL_SECONDS = 600


@dataclass
class ConfirmationRecord:
    """A stored, still-unanswered tool call awaiting explicit approval."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    conversation_id: str = ""
    agent: str = ""
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    tool_call_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "confirmation_id": self.id,
            "conversation_id": self.conversation_id,
            "agent": self.agent,
            "tool": self.tool_name,
            "arguments": self.arguments,
            "tool_call_id": self.tool_call_id,
        }


class ConfirmationService:
    """In-memory single-use store for pending tool confirmations."""

    def __init__(self, ttl_seconds: float = DEFAULT_CONFIRMATION_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, ConfirmationRecord] = {}

    def create(
        self,
        *,
        conversation_id: str,
        agent: str,
        tool_name: str,
        arguments: dict,
        tool_call_id: str,
    ) -> ConfirmationRecord:
        record = ConfirmationRecord(
            conversation_id=conversation_id,
            agent=agent,
            tool_name=tool_name,
            arguments=dict(arguments),
            tool_call_id=tool_call_id,
        )
        self._items[record.id] = record
        return record

    def get(self, confirmation_id: str) -> ConfirmationRecord | None:
        return self._items.get(confirmation_id)

    def _expired(self, record: ConfirmationRecord) -> bool:
        age = datetime.now(timezone.utc) - record.created_at
        return age.total_seconds() > self._ttl

    def consume(self, conversation_id: str, confirmation_id: str) -> ConfirmationRecord:
        """Return and delete a valid confirmation for ``conversation_id``.

        Raises:
            backend.core.errors.NotFoundError: unknown, expired, or owned by a
                different conversation.
        """
        record = self._items.get(confirmation_id)
        if record is None:
            raise NotFoundError(
                f"Confirmation '{confirmation_id}' not found.",
                detail={"available": sorted(self._items)},
            )
        if record.conversation_id != conversation_id:
            raise NotFoundError(
                "Confirmation does not belong to this conversation.",
                detail={"confirmation_conversation": record.conversation_id},
            )
        if self._expired(record):
            self._items.pop(confirmation_id, None)
            raise NotFoundError("Confirmation has expired.")
        self._items.pop(confirmation_id, None)
        return record

    def list_for(self, conversation_id: str) -> list[ConfirmationRecord]:
        return [r for r in self._items.values() if r.conversation_id == conversation_id]

    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        return count


@lru_cache
def get_confirmation_service() -> ConfirmationService:
    """Return the process-wide confirmation store (shared across requests)."""
    return ConfirmationService()