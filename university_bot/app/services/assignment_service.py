from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.models.assignment import Assignment


def _aware(dt: datetime | None) -> datetime | None:
    """Guarantee an aware datetime; naive values are assumed to be UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_local(dt: datetime | None, tzname: str | None = None) -> datetime | None:
    """Absolute stored timestamp -> wall clock in the student's timezone."""
    if dt is None:
        return None
    tz = ZoneInfo(tzname or get_settings().default_timezone)
    return _aware(dt).astimezone(tz)


def remaining_text(deadline: datetime | None) -> str:
    if deadline is None:
        return "No deadline"
    now = datetime.now(timezone.utc)
    delta = _aware(deadline) - now
    if delta.total_seconds() < 0:
        return "Overdue"
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return f"{days} days {hours} hours remaining"
    if hours > 0:
        return f"{hours} hours remaining"
    return f"{delta.seconds // 60} minutes remaining"


async def upcoming(session: AsyncSession, user_id: int, kind: str | None = None, limit: int = 20) -> list[Assignment]:
    q = (
        select(Assignment)
        .where(Assignment.user_id == user_id, Assignment.deadline.is_not(None))
        .where(Assignment.deadline >= datetime.now(timezone.utc))
        .order_by(Assignment.deadline)
        .limit(limit)
    )
    if kind:
        q = q.where(Assignment.kind == kind)
    result = await session.execute(q)
    from app.services.academic_sync import is_submitted

    return [a for a in result.scalars() if not is_submitted(a.submission_status)]


async def overdue(session: AsyncSession, user_id: int) -> list[Assignment]:
    result = await session.execute(
        select(Assignment)
        .where(Assignment.user_id == user_id, Assignment.deadline.is_not(None), Assignment.deadline < datetime.now(timezone.utc))
        .order_by(Assignment.deadline.desc())
        .limit(20)
    )
    return list(result.scalars())


def format_assignment(a: Assignment, tzname: str | None = None) -> str:
    local = to_local(a.deadline, tzname)
    deadline = f"{local:%B} {local.day}, {local:%H:%M}" if local else "-"
    return (
        f"📝 {a.course_name} — {a.title}\n\n"
        f"⏳ Deadline: {deadline}\n"
        f"🕙 {remaining_text(a.deadline)}\n"
        f"✅ Status: {a.submission_status or 'Not submitted'}"
    )
