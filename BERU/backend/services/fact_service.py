"""Long-term fact memory service.

Provides CRUD operations for durable user facts and preferences. Facts are
keyed by a unique string key and can be grouped by category.

Facts can be scoped to an agent and/or a project for isolation.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.context import get_current_principal
from backend.core.errors import NotFoundError
from backend.models.fact import Fact
from backend.services.scoping import owner_scope_user_id, scope_condition


class FactService:
    async def upsert(
        self,
        session: AsyncSession,
        *,
        key: str,
        value: str,
        category: str = "general",
        agent: str | None = None,
        project_id: str | None = None,
    ) -> Fact:
        """Create or update a fact. Returns the fact."""
        principal = get_current_principal()
        stmt = select(Fact).where(
            scope_condition(Fact, principal), Fact.key == key
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing is not None:
            existing.value = value
            existing.category = category
            existing.agent = agent
            existing.project_id = project_id
            await session.flush()
            return existing

        fact = Fact(
            key=key,
            value=value,
            category=category,
            agent=agent,
            project_id=project_id,
            user_id=owner_scope_user_id(principal),
        )
        session.add(fact)
        try:
            await session.flush()
            return fact
        except IntegrityError:
            # A concurrent writer inserted this key between our SELECT and
            # INSERT. Roll back the lost insert and update the committed row
            # (last-writer-wins) instead of surfacing a 500.
            await session.rollback()
            existing = (
                await session.execute(
                    select(Fact).where(
                        scope_condition(Fact, principal), Fact.key == key
                    )
                )
            ).scalar_one()
            existing.value = value
            existing.category = category
            existing.agent = agent
            existing.project_id = project_id
            await session.flush()
            return existing

    async def get(self, session: AsyncSession, key: str) -> Fact:
        """Retrieve a fact by key. Raises NotFoundError if missing."""
        stmt = select(Fact).where(
            scope_condition(Fact, get_current_principal()), Fact.key == key
        )
        fact = (await session.execute(stmt)).scalar_one_or_none()
        if fact is None:
            raise NotFoundError(f"Fact '{key}' not found.")
        return fact

    async def list(
        self,
        session: AsyncSession,
        *,
        category: str | None = None,
        agent: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Fact]]:
        """List facts, optionally filtered by category, agent, and/or project."""
        principal = get_current_principal()
        base = select(Fact).where(scope_condition(Fact, principal))
        count_stmt = select(Fact.id).where(scope_condition(Fact, principal))

        if category:
            base = base.where(Fact.category == category)
            count_stmt = count_stmt.where(Fact.category == category)
        if agent:
            base = base.where(Fact.agent == agent)
            count_stmt = count_stmt.where(Fact.agent == agent)
        if project_id:
            base = base.where(Fact.project_id == project_id)
            count_stmt = count_stmt.where(Fact.project_id == project_id)

        total = len((await session.execute(count_stmt)).all())

        stmt = base.order_by(Fact.updated_at.desc()).offset(offset).limit(limit)
        items = list((await session.execute(stmt)).scalars().all())
        return total, items

    async def delete(self, session: AsyncSession, key: str) -> None:
        """Delete a fact by key. Raises NotFoundError if missing."""
        fact = await self.get(session, key)
        await session.delete(fact)

    async def all_as_text(
        self,
        session: AsyncSession,
        *,
        agent: str | None = None,
        project_id: str | None = None,
        category: str | None = None,
        limit: int = 100,
    ) -> list[str]:
        """Return all facts as formatted strings for inclusion in LLM context.

        When agent, project_id, and/or category are provided, only matching
        facts are returned.
        """
        _, facts = await self.list(
            session,
            agent=agent,
            project_id=project_id,
            category=category,
            limit=limit,
        )
        return [f"{f.key}: {f.value}" for f in facts]
