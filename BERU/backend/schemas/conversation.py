"""Conversation schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.schemas.message import MessageRead


class ConversationRead(BaseModel):
    """Conversation summary (no messages)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None = None
    agent: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationRead):
    """Conversation including its ordered messages."""

    messages: list[MessageRead] = []


class ConversationList(BaseModel):
    """A page of conversation summaries."""

    total: int
    items: list[ConversationRead]
