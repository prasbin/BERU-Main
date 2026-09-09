"""ORM model for scheduled-task run history (append-only audit log).

Each row records one execution of a scheduled task: when it ran, the resulting
status (completed/failed), how long it took, and the error message when it
failed. History is owned by the task — deleting a task cascades to its runs.

``seq`` is a monotonic insert-order counter: it makes ``newest first``
ordering deterministic even when several rows share the same ``run_at`` and
``created_at`` timestamps (possible at microsecond precision), without relying
on the random row ``id`` for tie-breaking.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.scheduled_task import ScheduledTaskRecord


class TaskRunRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "task_runs"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    # Resulting engine status: "completed" | "failed".
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Wall-clock duration of the handler in milliseconds.
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Monotonic insert-order counter (assigned by record_task_run).
    seq: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    task: Mapped[ScheduledTaskRecord] = relationship(back_populates="runs")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<TaskRunRecord task_id={self.task_id!r} status={self.status!r}>"