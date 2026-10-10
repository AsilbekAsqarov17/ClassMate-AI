"""Class + deadline reminders. Called every minute by the scheduler."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.assignment import Assignment
from app.database.models.lesson import Lesson
from app.database.models.notification import NotificationRecord
from app.database.models.user import User

"""Deadline reminder thresholds, in hours before the deadline.

Exactly two reminders fire per item: 4 hours and 1 hour. Each threshold has
its own `kind` (deadline_4h / deadline_1h), which is half of the dedup
identity in NotificationRecord, so both reminders are delivered exactly once
for the same item and a restart never resends either.
"""
# Deadline reminder thresholds, in hours before the deadline.
#
# Exactly two reminders fire per item: 4 hours and 1 hour. Each threshold has
# its own `kind` (deadline_4h / deadline_1h), which is half of the dedup
# identity in NotificationRecord, so both reminders are delivered exactly once
# for the same item and a restart never resends either.
DEADLINE_REMINDER_HOURS = [4, 1]

# The scheduler polls once a minute, so an exact-instant match would miss
# anything that lands between two ticks. REMINDER_GRACE widens each window to
# absorb tick jitter.
REMINDER_GRACE = timedelta(minutes=5)

# An item discovered after its 4h instant has already passed (e.g. a first
# sync that ran late) must not produce a stream of overdue reminders. Past
# thresholds are skipped once they fall outside the grace window; a reminder
# still inside the grace window is honoured, because it is genuinely due.
DEADLINE_REMINDER_MAX_LAG = timedelta(minutes=10)


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


WEEKLY_ATTENDANCE_KIND = "weekly_attendance"


async def due_weekly_attendance(session: AsyncSession, user_id: int, week_ref: str) -> bool:
    """True (once) per user + report week. Mirrors the reminder dedup pattern."""
    return not await _already_sent(session, user_id, WEEKLY_ATTENDANCE_KIND, week_ref)


async def mark_weekly_attendance_sent(session: AsyncSession, user_id: int, week_ref: str) -> None:
    await _mark_sent(session, user_id, WEEKLY_ATTENDANCE_KIND, week_ref)


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
    """Deliver NEW SCORE messages; delete the item only after a successful send.

    Ordering is deliberate and load-bearing: the deduplication record is
    written ONLY after Telegram has accepted the message. If the send raises,
    nothing is recorded and the row is kept, so the next sync retries it.

    Known limitation (documented, not worked around): if Telegram accepts the
    message but the process dies before the commit below, the retry can
    duplicate that one message. Duplicating a score alert is preferable to
    silently dropping a grade, and the window is only the commit between two
    items.
    """
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
        # mark AFTER the send succeeded, keyed by external_id#score so a
        # corrected grade is not mistaken for an already-delivered one.
        from app.services.academic_sync import score_ref

        await mark("score", score_ref(row.external_id, row.score))
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
            kind = f"deadline_{interval}h"   # stable per-threshold dedup identity
            scheduled = deadline - timedelta(hours=interval)
            # Due inside the grace window, or slightly late inside the lag
            # window. Beyond that the reminder is stale and is dropped rather
            # than sent as an overdue notification.
            if not (scheduled <= now <= scheduled + REMINDER_GRACE + DEADLINE_REMINDER_MAX_LAG):
                continue
            if await _already_sent(session, user.id, kind, str(a.id)):
                continue   # already delivered: restart must not resend
            out.append((a, kind))
            await _mark_sent(session, user.id, kind, str(a.id))
    return out
