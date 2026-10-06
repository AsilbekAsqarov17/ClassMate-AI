"""Class + deadline reminders. Called every minute by the scheduler."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.assignment import Assignment
from app.database.models.lesson import Lesson
from app.database.models.notification import NotificationRecord
from app.database.models.user import User

DEADLINE_REMINDER_HOURS = [48, 1]
REMINDER_GRACE = timedelta(minutes=5)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=__import__("datetime").timezone.utc)
    return dt


async def _already_sent(session: AsyncSession, user_id: int, kind: str, ref_id: str) -> bool:
    result = await session.execute(
        select(NotificationRecord).where(
            NotificationRecord.user_id == user_id,
            NotificationRecord.kind == kind,
            NotificationRecord.ref_id == ref_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _mark_sent(session: AsyncSession, user_id: int, kind: str, ref_id: str) -> None:
    session.add(NotificationRecord(user_id=user_id, kind=kind, ref_id=ref_id, sent_at=datetime.now(__import__("datetime").timezone.utc)))
    await session.commit()


async def due_class_reminders(session: AsyncSession, user: User, now: datetime) -> list[tuple[Lesson, str]]:
    """Individual reminder for every lesson starting within [now, now + reminder_minutes] local."""
    if not user.class_notifications:
        return []
    if user.group_id is None:
        return []
    window_end = now + timedelta(minutes=user.reminder_minutes)
    result = await session.execute(
        select(Lesson).where(
            Lesson.group_id == user.group_id,
            Lesson.status == "active",
            Lesson.lesson_date == now.date(),
        )
    )
    out = []
    for l in result.scalars():
        start_dt = datetime.combine(l.lesson_date, l.start_time)
        if now <= start_dt <= window_end:
            kind = "class_reminder"
            ref = str(l.id)  # unique per lesson -> other classes are unaffected
            if not await _already_sent(session, user.id, kind, ref):
                out.append((l, kind))
                await _mark_sent(session, user.id, kind, ref)
    return out


async def due_daily_timetable(session: AsyncSession, user: User, now: datetime) -> list[Lesson]:
    """Daily summary: ALL of today's lessons, due exactly 1h before the first one.

    `now` is the student's LOCAL (naive) datetime. Returns the lesson list when
    the reminder is due, else []. Sent at most once per user per day
    (ref = the date, so different days are independent). No late sends: once
    the 1-hour mark has passed by more than REMINDER_GRACE, nothing is sent.
    """
    if not user.daily_timetable_notifications:
        return []
    if user.group_id is None:
        return []
    result = await session.execute(
        select(Lesson).where(
            Lesson.group_id == user.group_id,
            Lesson.status == "active",
            Lesson.lesson_date == now.date(),
        ).order_by(Lesson.start_time)
    )
    lessons = list(result.scalars())
    if not lessons:
        return []
    first_start = datetime.combine(now.date(), lessons[0].start_time)
    scheduled = first_start - timedelta(hours=1)
    if not (scheduled <= now <= scheduled + REMINDER_GRACE):
        return []
    kind = "daily_timetable"
    ref = now.date().isoformat()  # unique per day -> tomorrow fires again
    if await _already_sent(session, user.id, kind, ref):
        return []
    await _mark_sent(session, user.id, kind, ref)
    return lessons


async def send_score_notifications(session: AsyncSession, bot, user: User, items: list[Assignment]) -> None:
    """Deliver NEW SCORE messages; delete the item only after a successful send."""
    async def mark(kind: str, ref_id: str) -> None:
        session.add(NotificationRecord(user_id=user.id, kind=kind, ref_id=ref_id, sent_at=datetime.now(__import__("datetime").timezone.utc)))

    for row in items:
        max_score = row.max_score or "?"
        msg = (
            f"📊 NEW SCORE\n\n"
            f"📚 {row.course_name}\n"
            f"{'🧪' if row.kind == 'quiz' else '📝'} {row.title}\n\n"
            f"✅ Score: {row.score}/{max_score}"
        )
        try:
            await bot.send_message(user.telegram_id, msg)
        except Exception:
            continue  # keep the academic item for the next sync retry
        await mark("score", str(row.external_id))
        await session.delete(row)
    await session.commit()


async def due_deadline_reminders(session: AsyncSession, user: User, now: datetime) -> list[tuple[Assignment, str]]:
    if not user.deadline_notifications:
        return []
    result = await session.execute(
        select(Assignment).where(
            Assignment.user_id == user.id,
            Assignment.deadline.is_not(None),
            Assignment.deadline > now,
        )
    )
    out = []
    now = _aware(now)
    from app.services.academic_sync import is_submitted

    for a in result.scalars():
        if is_submitted(a.submission_status):
            continue  # completed items: no more deadline reminders
        if a.kind == "quiz" and a.open_time is not None and _aware(a.open_time) > now:
            continue  # quiz has not opened yet: no reminders until available
        deadline = _aware(a.deadline)
        for interval in DEADLINE_REMINDER_HOURS:
            kind = f"deadline_{interval}h"
            scheduled = deadline - timedelta(hours=interval)
            # fire only near the scheduled instant (± grace); never in the past
            if scheduled <= now <= scheduled + REMINDER_GRACE:
                if not await _already_sent(session, user.id, kind, str(a.id)):
                    out.append((a, kind))
                    await _mark_sent(session, user.id, kind, str(a.id))
    return out
