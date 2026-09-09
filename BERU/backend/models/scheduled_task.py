"""ORM model for durable scheduled tasks.

The scheduler engine operates on in-memory ``ScheduledTask`` value objects; this
record is the persistent mirror that lets provisioned tasks survive restarts.
Each field maps 1:1 to ``ScheduledTask`` (see ``backend/engines/scheduler.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.task_run import TaskRunRecord


class ScheduledTaskRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "scheduled_tasks"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    handler: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    task_type: Mapped[str] = mapped_column(
        String(16), default="one_shot", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )

    interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cron_expr: Mapped[str | None] = mapped_column(String(128), nullable=True)

    last_run: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_run: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)

    retention_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    keep_last: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Run history is owned by the task: deleting a task removes its runs via
    # the DB-level ``ON DELETE CASCADE`` (the pragma now turns foreign keys on).
    runs: Mapped[list[TaskRunRecord]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ScheduledTaskRecord id={self.id!r} name={self.name!r} status={self.status!r}>"