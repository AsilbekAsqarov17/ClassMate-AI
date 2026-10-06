from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.models.lesson import Lesson


def _tz(tzname: str | None = None) -> ZoneInfo:
    return ZoneInfo(tzname or get_settings().default_timezone)


async def _group_id(session: AsyncSession, user_id: int) -> int | None:
    from app.database.models.user import User

    u = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    return u.group_id if u else None


async def lessons_on(session: AsyncSession, user_id: int, d: date) -> list[Lesson]:
    gid = await _group_id(session, user_id)
    if gid is None:
        return []
    result = await session.execute(
        select(Lesson)
        .where(Lesson.group_id == gid, Lesson.lesson_date == d, Lesson.status == "active")
        .order_by(Lesson.start_time)
    )
    return list(result.scalars())


async def lessons_between(session: AsyncSession, user_id: int, start: date, end: date) -> list[Lesson]:
    gid = await _group_id(session, user_id)
    if gid is None:
        return []
    result = await session.execute(
        select(Lesson)
        .where(Lesson.group_id == gid, Lesson.lesson_date >= start, Lesson.lesson_date <= end)
        .order_by(Lesson.lesson_date, Lesson.start_time)
    )
    return list(result.scalars())


def today(user_tz: str | None = None) -> date:
    return datetime.now(_tz(user_tz)).date()


async def next_lesson(session: AsyncSession, user_id: int, tzname: str) -> tuple[Lesson, timedelta] | None:
    gid = await _group_id(session, user_id)
    if gid is None:
        return None
    now = datetime.now(_tz(tzname)).replace(tzinfo=None)
    result = await session.execute(
        select(Lesson)
        .where(Lesson.group_id == gid, Lesson.status == "active")
        .where((Lesson.lesson_date > now.date()) | ((Lesson.lesson_date == now.date()) & (Lesson.end_time > now.time())))
        .order_by(Lesson.lesson_date, Lesson.start_time)
        .limit(1)
    )
    lesson = result.scalar_one_or_none()
    if lesson is None:
        return None
    start_dt = datetime.combine(lesson.lesson_date, lesson.start_time)
    return lesson, start_dt - now


def format_lesson(l: Lesson) -> str:
    lines = [f"📚 {l.course_name}"]
    if l.professor:
        lines.append(f"👨‍🏫 {l.professor}")
    if l.room:
        lines.append(f"🏫 {l.room}")
    lines.append(f"🕐 {l.start_time:%H:%M} — {l.end_time:%H:%M}")
    return "\n".join(lines)


def format_day(lessons: list[Lesson], d: date) -> str:
    if not lessons:
        return "🎉 No classes scheduled."
    parts = [f"📅 {d:%A, %B %d}\n"]
    for l in lessons:
        parts.append(format_lesson(l) + "\n")
    return "\n".join(parts)
