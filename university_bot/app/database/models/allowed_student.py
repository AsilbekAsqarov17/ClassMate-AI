from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class AllowedStudent(Base, TimestampMixin):
    """Student IDs permitted to register with the bot.

    The table pre-dates this model (it was created by a migration that is not
    in the repository, and the DB is stamped at revision 0004 while only 0001
    and 0002 are committed here). Mapping it now makes the allowlist actually
    enforceable instead of dead weight.

    `create_all` is a no-op for an existing table, so declaring the model does
    not touch or rewrite the rows already in PostgreSQL.
    """

    __tablename__ = "allowed_student_ids"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)