"""Fact schemas for the long-term memory API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FactCreate(BaseModel):
    """Request body to add or update a fact."""

    key: str = Field(..., min_length=1, max_length=255, description="Unique fact identifier.")
    value: str = Field(..., min_length=1, max_length=5000, description="The fact content.")
    category: str = Field(
        default="general",
        max_length=64,
        description="Category for grouping (e.g. 'preference', 'identity').",
    )


class FactRead(BaseModel):
    """A stored fact."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    value: str
    category: str
    created_at: datetime
    updated_at: datetime


class FactList(BaseModel):
    """A list of facts."""

    total: int
    items: list[FactRead]
