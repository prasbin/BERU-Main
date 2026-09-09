"""Chat endpoint schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from backend.schemas.common import Usage
from backend.schemas.message import MessageRead


class ChatRequest(BaseModel):
    """A user message sent to BERU."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=32_000,
        description="The user's message text.",
    )
    conversation_id: str | None = Field(
        None, description="Existing conversation to continue. Omit to start a new one."
    )
    agent: str | None = Field(
        None, description="Agent to handle the message. Defaults to the core agent."
    )
    title: str | None = Field(None, description="Optional title when creating a new conversation.")
    project_id: str | None = Field(
        None,
        description=(
            "Optional project to scope a new conversation to. Existing "
            "conversations keep their own project regardless of this value."
        ),
    )


class ConfirmationRequest(BaseModel):
    """Resolve a pending tool call (approve or deny)."""

    conversation_id: str = Field(..., description="Conversation the confirmation belongs to.")
    confirmation_id: str = Field(..., description="The pending confirmation id to resolve.")


class DenyResponse(BaseModel):
    """Result of denying a pending tool call."""

    status: Literal["denied"] = "denied"
    conversation_id: str
    confirmation_id: str
    agent: str
    tool: str


class ChatResponse(BaseModel):
    """BERU's reply plus context needed to continue the conversation."""

    conversation_id: str
    agent: str
    message: MessageRead
    model: str | None = None
    usage: Usage | None = None
    pending_confirmations: list[dict] | None = Field(
        None,
        description=(
            "Tool calls awaiting user confirmation. When present, the caller "
            "should surface them and POST /chat/confirm (per id) to approve."
        ),
    )
