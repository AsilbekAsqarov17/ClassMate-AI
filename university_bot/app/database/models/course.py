from datetime import date, time

from sqlalchemy import Date, ForeignKey, Index, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class Course(Base, TimestampMixin):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(512))
    professor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # OFFLINE/ONLINE
