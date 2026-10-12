"""Weekly-report delivery ledger.

Idempotency lives in the database, not in memory: a unique constraint on
(user_id, week_start) means a retried job, a restart, or a second worker
cannot produce a second report for the same week.
"""

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class AttendanceReportDelivery(Base, TimestampMixin):
    __tablename__ = "attendance_report_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Monday of the reported week; identifies the reporting period.
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once the row exists so a send failure can be retried, but a *sent*
    # report is never re-sent.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    __table_args__ = (
        UniqueConstraint("user_id", "week_start", name="uq_attendance_report_week"),
    )


def mark_sent() -> datetime:
    return datetime.now(timezone.utc)