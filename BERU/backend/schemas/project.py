"""Project schemas for the project management API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """Request body to create or update a project."""

    name: str = Field(..., min_length=1, max_length=255, description="Unique project name.")
    description: str | None = Field(
        default=None, max_length=1000, description="Optional project description."
    )


class ProjectRead(BaseModel):
    """A stored project."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ProjectList(BaseModel):
    """A list of projects."""

    total: int
    items: list[ProjectRead]
