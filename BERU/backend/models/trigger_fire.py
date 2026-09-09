"""ORM model for monitor-trigger fire history (append-only audit log).

Each row records one firing of a monitor trigger: when it fired, the condition
that was true, and the source value that satisfied it. History is owned by the
trigger — deleting a trigger cascades to its fires.

``seq`` is a monotonic insert-order counter: it makes ``newest first``
ordering deterministic even when several rows share the same ``fired_at`` and
``created_at`` timestamps (possible at microsecond precision), without relying
on the random row ``id`` for tie-breaking.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.monitor_trigger import MonitorTriggerRecord


class TriggerFireRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "trigger_fires"

    trigger_id: Mapped[str] = mapped_column(
        ForeignKey("monitor_triggers.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    # Snapshot of the condition dict (same shape as trigger.to_dict()["condition"]).
    condition: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # The current source value that satisfied the condition (any JSON value).
    value: Mapped[object | None] = mapped_column(JSON, nullable=True)
    # Monotonic insert-order counter (assigned by record_trigger_fire).
    seq: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    trigger: Mapped[MonitorTriggerRecord] = relationship(back_populates="fires")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<TriggerFireRecord trigger_id={self.trigger_id!r} at={self.fired_at!r}>"