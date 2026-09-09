"""ORM model for one tool-call activity record (durable reliability ledger).

Each row mirrors one :class:`backend.services.activity_ledger.ActivityEntry`:
the tool that ran, the outcome, how long it took, and the per-request
correlation id, so observability data survives process restarts. A ``seq``
column (assigned in Python at persist time) breaks ties between rows sharing
the same wall-clock ``timestamp``; ordering/restore keys on ``timestamp``
because ``seq`` resets on every process restart.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin


class ActivityRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "activity_records"
    __table_args__ = (
        CheckConstraint("seq >= 0", name="ck_activity_records_seq_nonneg"),
    )

    seq: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    timestamp: Mapped[float] = mapped_column(Float, index=True, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    agent: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    args_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ActivityRecord id={self.id!r} tool={self.tool_name!r} "
            f"outcome={self.outcome!r}>"
        )