"""Message schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MessageRead(BaseModel):
    """A message as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    model: str | None = None
    token_count: int | None = None
    created_at: datetime
