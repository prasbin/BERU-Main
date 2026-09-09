"""Long-term memory endpoints: add, retrieve, list, and forget facts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.security import require_api_key
from backend.database.base import get_session
from backend.schemas.fact import FactCreate, FactList, FactRead
from backend.services.fact_service import FactService

router = APIRouter(
    prefix="/facts", tags=["memory"], dependencies=[Depends(require_api_key)]
)

_fact_service = FactService()


@router.post(
    "",
    response_model=FactRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add or update a fact",
)
async def upsert_fact(
    body: FactCreate,
    session: AsyncSession = Depends(get_session),
) -> FactRead:
    """Create a new fact or update an existing one with the same key."""
    fact = await _fact_service.upsert(
        session,
        key=body.key,
        value=body.value,
        category=body.category,
    )
    await session.commit()
    return FactRead.model_validate(fact)


@router.get("", response_model=FactList, summary="List all facts")
async def list_facts(
    category: str | None = Query(None, description="Filter by category"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FactList:
    total, items = await _fact_service.list(
        session, category=category, limit=limit, offset=offset
    )
    return FactList(
        total=total,
        items=[FactRead.model_validate(f) for f in items],
    )


@router.get("/{key}", response_model=FactRead, summary="Get a fact by key")
async def get_fact(
    key: str,
    session: AsyncSession = Depends(get_session),
) -> FactRead:
    fact = await _fact_service.get(session, key)
    return FactRead.model_validate(fact)


@router.delete(
    "/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a fact (forget)",
)
async def delete_fact(
    key: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _fact_service.delete(session, key)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
