from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base


class NotificationRecord(Base):
    """One row per *sent* notification — backs duplicate prevention."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # class_reminder / deadline_3d / deadline_24h / deadline_3h
    ref_id: Mapped[str] = mapped_column(String(128), index=True)  # lesson.id or assignment.id
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "ref_id", name="uq_notification_once"),
    )
