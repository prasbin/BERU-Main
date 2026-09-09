"""ORM model for durable monitor triggers.

Mirrors the engine's in-memory ``Trigger`` value object (see
``backend/engines/monitor.py``) so configured monitoring survives restarts. The
``condition`` and ``actions`` fields are stored as JSON and round-trip through
``TriggerCondition`` / the trigger's action list unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.models.trigger_fire import TriggerFireRecord


class MonitorTriggerRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "monitor_triggers"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    condition: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    actions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    last_fired: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fire_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cooldown_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    retention_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    keep_last: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Fire history is owned by the trigger: deleting a trigger removes its
    # fires via the DB-level ``ON DELETE CASCADE``.
    fires: Mapped[list[TriggerFireRecord]] = relationship(
        back_populates="trigger",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<MonitorTriggerRecord id={self.id!r} name={self.name!r} status={self.status!r}>"