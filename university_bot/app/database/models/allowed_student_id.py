from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class AllowedStudentID(Base, TimestampMixin):
    __tablename__ = "allowed_student_ids"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
