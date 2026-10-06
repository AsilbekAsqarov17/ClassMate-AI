from datetime import date, time

from sqlalchemy import Date, ForeignKey, Index, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class Lesson(Base, TimestampMixin):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    course_name: Mapped[str] = mapped_column(String(512))
    professor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    room: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lesson_date: Mapped[date] = mapped_column(Date, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active/cancelled

    __table_args__ = (Index("ix_lessons_group_date", "group_id", "lesson_date"),)
