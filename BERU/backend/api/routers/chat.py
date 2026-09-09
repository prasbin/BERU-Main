"""Chat endpoints: send a message and receive BERU's reply (buffered or streamed)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_chat_service
from backend.api.rate_limit import rate_limit_chat
from backend.api.security import require_api_key
from backend.core.config import Settings, get_settings
from backend.database.base import get_session
from backend.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ConfirmationRequest,
    DenyResponse,
)
from backend.schemas.common import Usage
from backend.schemas.message import MessageRead
from backend.services.chat_service import ChatService, ChatStreamEvent

router = APIRouter(
    tags=["chat"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_chat)],
)


@router.post("/chat", response_model=ChatResponse, summary="Send a message to BERU")
async def chat(
    request: ChatRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    chat_service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    outcome = await chat_service.process(session, settings, request)

    usage = None
    if outcome.result.usage is not None:
        usage = Usage(
            prompt_tokens=outcome.result.usage.prompt_tokens,
            completion_tokens=outcome.result.usage.completion_tokens,
            total_tokens=outcome.result.usage.total_tokens,
        )

    return ChatResponse(
        conversation_id=outcome.conversation.id,
        agent=outcome.result.agent,
        message=MessageRead.model_validate(outcome.assistant_message),
        model=outcome.result.model,
        usage=usage,
        pending_confirmations=outcome.pending_confirmations,
    )


@router.post(
    "/chat/confirm",
    response_model=ChatResponse,
    summary="Approve a pending tool call",
)
async def confirm_tool(
    request: ConfirmationRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    chat_service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    """Execute a previously pending (approval-gated) tool call.

    Approving is single-use: the confirmation is consumed, the recorded tool
    call runs with authorization, and the agent replies grounded in the result.
    """
    outcome = await chat_service.confirm_tool_call(
        session,
        settings,
        conversation_id=request.conversation_id,
        confirmation_id=request.confirmation_id,
    )

    usage = None
    if outcome.result.usage is not None:
        usage = Usage(
            prompt_tokens=outcome.result.usage.prompt_tokens,
            completion_tokens=outcome.result.usage.completion_tokens,
            total_tokens=outcome.result.usage.total_tokens,
        )

    return ChatResponse(
        conversation_id=outcome.conversation.id,
        agent=outcome.result.agent,
        message=MessageRead.model_validate(outcome.assistant_message),
        model=outcome.result.model,
        usage=usage,
        pending_confirmations=outcome.pending_confirmations,
    )


@router.post(
    "/chat/deny",
    response_model=DenyResponse,
    summary="Deny a pending tool call",
)
async def deny_tool(
    request: ConfirmationRequest,
    chat_service: ChatService = Depends(get_chat_service),
) -> DenyResponse:
    """Refuse a previously pending tool call without executing it.

    Denying is single-use, exactly like approving: the confirmation is consumed,
    so neither this id nor a replayed request can ever run the tool.
    """
    record = chat_service.deny_tool_call(
        request.conversation_id,
        request.confirmation_id,
    )
    return DenyResponse(
        conversation_id=request.conversation_id,
        confirmation_id=record.id,
        agent=record.agent,
        tool=record.tool_name,
    )


@router.get(
    "/chat/confirmations",
    summary="List pending tool confirmations for a conversation",
)
async def list_pending(
    conversation_id: str,
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    """Return unanswered tool confirmations (for a confirmation UI)."""
    return {
        "pending_confirmations": chat_service.list_pending_confirmations(
            conversation_id
        )
    }


def _format_sse(event: ChatStreamEvent) -> str:
    """Render a :class:`ChatStreamEvent` as one Server-Sent Events block."""
    if event.kind == "start":
        payload: dict = {
            "conversation_id": event.conversation_id,
            "agent": event.agent,
        }
    elif event.kind == "delta":
        payload = {"text": event.text or ""}
    elif event.kind == "end":
        usage = None
        if event.usage is not None:
            usage = {
                "prompt_tokens": event.usage.prompt_tokens,
                "completion_tokens": event.usage.completion_tokens,
                "total_tokens": event.usage.total_tokens,
            }
        payload = {
            "conversation_id": event.conversation_id,
            "agent": event.agent,
            "message_id": event.message_id,
            "model": event.model,
            "usage": usage,
            "pending_confirmations": event.pending_confirmations or [],
        }
    else:  # "error"
        payload = {"error": event.error or "Generation failed."}

    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event.kind}\ndata: {data}\n\n"


@router.post(
    "/chat/stream",
    summary="Stream a message to BERU (Server-Sent Events)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat_stream(
    request: ChatRequest,
    settings: Settings = Depends(get_settings),
    chat_service: ChatService = Depends(get_chat_service),
) -> StreamingResponse:
    """Stream BERU's reply as SSE.

    Emits an ``event: start`` block, then incremental ``event: delta`` blocks,
    then a final ``event: end`` block (or ``event: error`` on mid-stream
    failure). The first event is produced eagerly, so an unknown conversation or
    agent raises here and is returned as a normal JSON error (e.g. 404) before
    the stream starts. ``stream_process`` manages its own short-lived database
    sessions, so no session is injected here.
    """
    stream = chat_service.stream_process(settings, request)
    first_event = await stream.__anext__()  # may raise NotFoundError -> 404

    async def event_source() -> AsyncIterator[str]:
        yield _format_sse(first_event)
        async for event in stream:
            yield _format_sse(event)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering (e.g. nginx) so tokens flush immediately.
            "X-Accel-Buffering": "no",
        },
    )
