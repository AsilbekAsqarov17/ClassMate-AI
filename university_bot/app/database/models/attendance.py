import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class Attendance(Base, TimestampMixin):
    """Latest successfully fetched E-Class attendance per user/course."""

    __tablename__ = "attendance"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    course_external_id: Mapped[str] = mapped_column(String(64), index=True)
    course_name: Mapped[str] = mapped_column(String(512))
    # True  → E-Class recorded attendance (counts below are real)
    # False → genuinely blank/unrecorded in E-Class (counts stay NULL)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    absences: Mapped[int | None] = mapped_column(Integer, nullable=True)
    present: Mapped[int | None] = mapped_column(Integer, nullable=True)
    late: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excused: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON list of {"date": ..., "status": ...}; NULL when unknown
    records_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # last successful fetch

    __table_args__ = (
        UniqueConstraint("user_id", "course_external_id", name="uq_attendance_user_course"),
    )

    def records(self) -> list[dict]:
        if not self.records_json:
            return []
        try:
            return json.loads(self.records_json)
        except (ValueError, TypeError):
            return []
