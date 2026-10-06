"""Quiz = Assignment rows with kind='quiz'; this service formats/queries them."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.assignment import Assignment
from app.services.assignment_service import remaining_text, to_local


def is_available(q: Assignment, now: datetime | None = None) -> bool:
    """A quiz is available once its E-Class open_time has passed.

    open_time None = availability unknown -> treat as available (deadline
    filtering still applies).
    """
    if q.open_time is None:
        return True
    ot = q.open_time if q.open_time.tzinfo else q.open_time.replace(tzinfo=timezone.utc)
    return ot <= (now or datetime.now(timezone.utc))


async def upcoming_quizzes(session: AsyncSession, user_id: int) -> list[Assignment]:
    """Currently available, not completed quizzes only."""
    result = await session.execute(
        select(Assignment)
        .where(Assignment.user_id == user_id, Assignment.kind == "quiz", Assignment.deadline >= datetime.now(timezone.utc))
        .order_by(Assignment.deadline)
    )
    from app.services.academic_sync import is_submitted

    return [
        q for q in result.scalars()
        if not is_submitted(q.submission_status) and is_available(q)
    ]


def format_quiz(q: Assignment, tzname: str | None = None) -> str:
    local = to_local(q.deadline, tzname)
    deadline = f"{local:%B} {local.day}, {local:%H:%M}" if local else "-"
    return f"🧪 {q.course_name} — {q.title}\n⏳ {deadline}\n🕙 {remaining_text(q.deadline)}"
