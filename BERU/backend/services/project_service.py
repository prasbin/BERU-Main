"""Project management service.

Provides CRUD operations for projects. Projects are containers for
conversations and scoped facts.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.context import get_current_principal, is_scoped
from backend.core.errors import NotFoundError
from backend.models.project import Project
from backend.services.scoping import owner_scope_user_id, scope_condition


class ProjectService:
    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        description: str | None = None,
    ) -> Project:
        """Create a new project. Raises if the name already exists (per user)."""
        existing = await self.get_by_name(session, name)
        if existing is not None:
            from backend.core.errors import BadRequestError

            raise BadRequestError(f"Project '{name}' already exists.")

        project = Project(
            name=name,
            description=description,
            user_id=owner_scope_user_id(get_current_principal()),
        )
        session.add(project)
        await session.flush()
        return project

    async def get(self, session: AsyncSession, project_id: str) -> Project:
        """Retrieve a project by ID. Raises NotFoundError if missing."""
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"Project '{project_id}' not found.")
        principal = get_current_principal()
        if is_scoped(principal) and project.user_id != principal.user_id:
            raise NotFoundError(f"Project '{project_id}' not found.")
        return project

    async def get_by_name(self, session: AsyncSession, name: str) -> Project | None:
        """Retrieve a project by name (scoped to the current user). None if absent."""
        stmt = select(Project).where(
            scope_condition(Project, get_current_principal()),
            Project.name == name,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        session: AsyncSession,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Project]]:
        """List projects (scoped to the current user). Returns (total, items)."""
        principal = get_current_principal()
        total = len(
            (
                await session.execute(
                    select(Project.id).where(scope_condition(Project, principal))
                )
            ).all()
        )
        stmt = (
            select(Project)
            .where(scope_condition(Project, principal))
            .order_by(Project.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        items = list((await session.execute(stmt)).scalars().all())
        return total, items

    async def update(
        self,
        session: AsyncSession,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Project:
        """Update a project. Raises NotFoundError if missing."""
        project = await self.get(session, project_id)
        if name is not None:
            project.name = name
        if description is not None:
            project.description = description
        await session.flush()
        return project

    async def delete(self, session: AsyncSession, project_id: str) -> None:
        """Delete a project. Raises NotFoundError if missing."""
        project = await self.get(session, project_id)
        await session.delete(project)
