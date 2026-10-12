"""Registration allowlist: only pre-approved Student IDs may connect.

The bot is scoped to one study group, so `/start` must not accept an arbitrary
E-Class login. Validation only proves the ID is *well formed* (`u2` + 6 digits);
membership is a separate, explicit question answered by the
`allowed_student_ids` table.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.allowed_student import AllowedStudent

log = logging.getLogger(__name__)

NOT_ALLOWED_MSG = (
    "This Student ID is not registered for ClassMate AI. "
    "Please ask Asqarbek to add your Student ID."
)


async def is_allowed(session: AsyncSession, student_id: str) -> bool:
    """True when `student_id` (already normalized) is on the allowlist.

    Case-insensitive, because E-Class IDs are stored lowercase by
    `normalize_student_id` while a human may type "U2410037".

    An empty table is treated as *deny-all* and logged loudly: silently
    accepting everyone because the table went missing would defeat the entire
    purpose of the check.
    """
    normalized = student_id.strip().lower()
    found = (
        await session.execute(
            select(AllowedStudent.id).where(
                func.lower(AllowedStudent.student_id) == normalized
            )
        )
    ).scalar_one_or_none()
    if found is not None:
        return True

    total = (await session.execute(select(func.count()).select_from(AllowedStudent))).scalar_one()
    if total == 0:
        log.error(
            "allowed_student_ids is empty - every registration will be rejected. "
            "Populate the table to restore access."
        )
    return False


async def allowlist_size(session: AsyncSession) -> int:
    """Number of Student IDs currently on the allowlist."""
    return (await session.execute(select(func.count()).select_from(AllowedStudent))).scalar_one()