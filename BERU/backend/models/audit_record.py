"""ORM model for one approval/denial decision (durable reliability audit).

Each row mirrors one :class:`backend.services.activity_ledger.AuditEntry`:
the tool call the user decided on, the decision, and the single-use
confirmation id that was consumed, so the approval trail survives restarts. A
``seq`` column breaks ties between rows sharing the same wall-clock
``timestamp``; ordering/restore keys on ``timestamp`` because ``seq`` resets
on every process restart.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.models.mixins import TimestampMixin, UUIDMixin


class AuditRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "audit_records"
    __table_args__ = (
        CheckConstraint("seq >= 0", name="ck_audit_records_seq_nonneg"),
    )

    seq: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    timestamp: Mapped[float] = mapped_column(Float, index=True, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    agent: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    decision: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    confirmation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<AuditRecord id={self.id!r} tool={self.tool_name!r} "
            f"decision={self.decision!r}>"
        )