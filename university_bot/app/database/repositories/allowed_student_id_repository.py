from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.allowed_student_id import AllowedStudentID


class AllowedStudentIDRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def is_allowed(self, student_id: str) -> bool:
        """True iff the normalized (lowercase) student_id is whitelisted."""
        result = await self.session.execute(
            select(AllowedStudentID.id).where(AllowedStudentID.student_id == student_id)
        )
        return result.first() is not None
