from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class EClassAccount(Base, TimestampMixin):
    __tablename__ = "eclass_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # ONE-TIME PASSWORD DESIGN: the E-Class password is used once at login and
    # is NEVER persisted. Only the resulting authenticated session (cookies)
    # is stored here, encrypted at rest with Fernet.
    session_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted cookies
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
