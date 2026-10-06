from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class Assignment(Base, TimestampMixin):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    course_name: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(32), default="homework")  # homework/assignment/project/quiz/exam/other
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # quizzes: when it becomes available
    submission_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_score: Mapped[str | None] = mapped_column(String(32), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    __table_args__ = (Index("ix_assignments_user_deadline", "user_id", "deadline"),)
