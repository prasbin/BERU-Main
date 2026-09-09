"""Shared schema fragments used across multiple endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Usage(BaseModel):
    """Token accounting for a single LLM call."""

    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    total_tokens: int = Field(0, ge=0)


class ErrorBody(BaseModel):
    type: str
    message: str
    detail: object | None = None


class ErrorResponse(BaseModel):
    """Documents the standard error envelope returned by the API."""

    error: ErrorBody
