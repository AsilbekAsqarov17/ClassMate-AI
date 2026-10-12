"""Attendance persistence.

Both tables already exist in the deployed database (created by a migration that
is not present in the repository). The models below mirror the live schema
exactly — including `attendance.available`, which is what lets the app
distinguish "no absences recorded" from "attendance could not be retrieved".

Attendance is an append-only history, unlike assignments (a watchlist): a
session stays meaningful after the semester moves on, so rows are only removed
by the retention window in attendance_service.sync_attendance_records().
"""

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin
from app.eclass.models import STATUS_UNKNOWN


class AttendanceRecord(Base, TimestampMixin):
    """One attendance session as E-Class reported it for this student."""

    __tablename__ = "attendance_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    course_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    course_name: Mapped[str] = mapped_column(String(512), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time | None] = mapped_column(nullable=True)
    end_time: Mapped[time | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=STATUS_UNKNOWN, nullable=False)
    raw_status: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        # One row per session: this is what makes re-syncing idempotent.
        UniqueConstraint("user_id", "external_id", name="uq_attendance_user_session"),
        Index("ix_attendance_user_date", "user_id", "session_date"),
        # Mirrors the indexes already present on the deployed table.
        Index("ix_attendance_records_session_date", "session_date"),
        Index("ix_attendance_records_external_id", "external_id"),
    )


class Attendance(Base, TimestampMixin):
    """Per-course availability + counters for the latest sync.

    `available=False` means E-Class gave us nothing usable for this course;
    the counters are then NULL and must be shown as "unavailable", not "0".
    """

    __tablename__ = "attendance"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    course_external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    course_name: Mapped[str] = mapped_column(String(512), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    absences: Mapped[int | None] = mapped_column(Integer, nullable=True)
    present: Mapped[int | None] = mapped_column(Integer, nullable=True)
    late: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excused: Mapped[int | None] = mapped_column(Integer, nullable=True)
    records_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Explicit TIMESTAMPTZ: the live column is `timestamp with time zone`, and
    # inference from a bare `datetime` would produce a naive TIMESTAMP instead.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "course_external_id", name="uq_attendance_user_course"),
        Index("ix_attendance_course_external_id", "course_external_id"),
    )