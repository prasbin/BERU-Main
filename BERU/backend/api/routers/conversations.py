"""Conversation history endpoints: list, retrieve, and delete conversations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_conversation_service
from backend.api.security import require_api_key
from backend.database.base import get_session
from backend.schemas.conversation import (
    ConversationDetail,
    ConversationList,
    ConversationRead,
)
from backend.schemas.message import MessageRead
from backend.services.conversation_service import ConversationService

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", response_model=ConversationList, summary="List conversations")
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    conversations: ConversationService = Depends(get_conversation_service),
) -> ConversationList:
    total, items = await conversations.list(session, limit=limit, offset=offset)
    return ConversationList(
        total=total,
        items=[ConversationRead.model_validate(c) for c in items],
    )


@router.get("/{conversation_id}", response_model=ConversationDetail, summary="Get a conversation")
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    conversations: ConversationService = Depends(get_conversation_service),
) -> ConversationDetail:
    conversation = await conversations.get(session, conversation_id)
    messages = await conversations.get_messages(session, conversation_id)
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        agent=conversation.agent,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[MessageRead.model_validate(m) for m in messages],
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=list[MessageRead],
    summary="Get a conversation's messages",
)
async def get_conversation_messages(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    conversations: ConversationService = Depends(get_conversation_service),
) -> list[MessageRead]:
    # Ensure the conversation exists (raises 404 otherwise).
    await conversations.get(session, conversation_id)
    messages = await conversations.get_messages(session, conversation_id)
    return [MessageRead.model_validate(m) for m in messages]


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation",
)
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    conversations: ConversationService = Depends(get_conversation_service),
) -> Response:
    conversation = await conversations.get(session, conversation_id)
    await session.delete(conversation)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
