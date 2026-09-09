"""Project management endpoints: create, list, get, update, delete projects."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.security import require_api_key
from backend.database.base import get_session
from backend.schemas.project import ProjectCreate, ProjectList, ProjectRead
from backend.services.project_service import ProjectService

router = APIRouter(
    prefix="/projects", tags=["projects"], dependencies=[Depends(require_api_key)]
)

_project_service = ProjectService()


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    body: ProjectCreate,
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    """Create a new project."""
    project = await _project_service.create(
        session,
        name=body.name,
        description=body.description,
    )
    await session.commit()
    return ProjectRead.model_validate(project)


@router.get("", response_model=ProjectList, summary="List all projects")
async def list_projects(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ProjectList:
    total, items = await _project_service.list(session, limit=limit, offset=offset)
    return ProjectList(
        total=total,
        items=[ProjectRead.model_validate(p) for p in items],
    )


@router.get("/{project_id}", response_model=ProjectRead, summary="Get a project")
async def get_project(
    project_id: str,
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    project = await _project_service.get(session, project_id)
    return ProjectRead.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectRead, summary="Update a project")
async def update_project(
    project_id: str,
    body: ProjectCreate,
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    project = await _project_service.update(
        session,
        project_id,
        name=body.name,
        description=body.description,
    )
    await session.commit()
    return ProjectRead.model_validate(project)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project",
)
async def delete_project(
    project_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _project_service.delete(session, project_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
