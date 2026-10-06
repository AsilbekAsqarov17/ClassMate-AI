from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, TimestampMixin
from app.database.models.group import Group


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tashkent", nullable=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True, index=True)
    group: Mapped[Group | None] = relationship(lazy="selectin")

    class_notifications: Mapped[bool] = mapped_column(default=True, nullable=False)
    deadline_notifications: Mapped[bool] = mapped_column(default=True, nullable=False)
    daily_timetable_notifications: Mapped[bool] = mapped_column(default=True, nullable=False)
    reminder_minutes: Mapped[int] = mapped_column(default=15, nullable=False)
