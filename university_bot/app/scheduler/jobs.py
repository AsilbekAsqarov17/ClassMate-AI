"""APScheduler-based background jobs (notifications + periodic sync)."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.database import get_session_factory
from app.database.models.eclass_account import EClassAccount
from app.database.models.user import User
from app.services import notification_service, sync_service

log = logging.getLogger(__name__)


def build_deadline_message(assignment, interval_kind: str, tzname: str | None) -> str:
    from app.services.assignment_service import to_local

    local = to_local(assignment.deadline, tzname)
    deadline = f"{local:%B} {local.day}, {local:%H:%M}" if local else "-"
    is_quiz = getattr(assignment, "kind", "") == "quiz"
    item_emoji = "🧪" if is_quiz else "📝"
    kind_word = "QUIZ" if is_quiz else "ASSIGNMENT"
    hours = interval_kind.replace("deadline_", "").replace("h", "")
    if hours == "1":
        return (
            f"🚨 {kind_word} DEADLINE IN 1 HOUR\n\n"
            f"📚 Course: {assignment.course_name}\n"
            f"{item_emoji} {assignment.title}\n\n"
            f"⏰ Deadline: {deadline}"
        )
    return (
        f"⏰ {kind_word} DEADLINE REMINDER\n\n"
        f"📚 Course: {assignment.course_name}\n"
        f"{item_emoji} {assignment.title}\n\n"
        f"⏳ Deadline: {deadline}\n"
        f"🕐 {hours} hours remaining"
    )


_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def build_daily_timetable_message(lessons) -> str:
    """Full-day summary sent 1 hour before the first class."""
    n = len(lessons)
    parts = [
        "📅 TODAY'S TIMETABLE\n",
        f"You have {n} class{'es' if n != 1 else ''} today:\n",
    ]
    for i, l in enumerate(lessons, 1):
        num = _NUM_EMOJI[i - 1] if i <= len(_NUM_EMOJI) else f"{i}."
        parts.append(
            f"\n{num} {l.start_time:%H:%M}–{l.end_time:%H:%M}\n"
            f"📚 {l.course_name}\n"
            f"👨‍🏫 {l.professor or '—'}\n"
            f"🏫 {l.room or '—'}"
        )
    return "\n".join(parts)


def build_class_reminder_message(lesson, reminder_minutes: int) -> str:
    return (
        f"⏰ CLASS STARTING IN {reminder_minutes} MINUTES\n\n"
        f"📚 {lesson.course_name}\n"
        f"👨‍🏫 {lesson.professor or '—'}\n"
        f"🏫 {lesson.room or '—'}\n\n"
        f"🕐 {lesson.start_time:%H:%M}–{lesson.end_time:%H:%M}"
    )


async def check_notifications(bot) -> None:
    now_utc = datetime.now(__import__("datetime").timezone.utc)
    async with get_session_factory()() as session:
        result = await session.execute(
            select(User).join(EClassAccount, EClassAccount.user_id == User.id).where(EClassAccount.is_active.is_(True))
        )
        users = list(result.scalars())
        for user in users:
            tz = ZoneInfo(user.timezone or get_settings().default_timezone)
            local_now = now_utc.replace(tzinfo=__import__("datetime").timezone.utc).astimezone(tz).replace(tzinfo=None)
            # 1) daily timetable summary: 1 hour before the FIRST class
            daily = await notification_service.due_daily_timetable(session, user, local_now)
            if daily:
                try:
                    await bot.send_message(user.telegram_id, build_daily_timetable_message(daily))
                except Exception as exc:
                    log.warning("daily timetable send failed: %s", exc)
            # 2) individual reminder: N minutes before EVERY class
            for lesson, _ in await notification_service.due_class_reminders(session, user, local_now):
                try:
                    await bot.send_message(
                        user.telegram_id,
                        build_class_reminder_message(lesson, user.reminder_minutes),
                    )
                except Exception as exc:  # user blocked bot, etc.
                    log.warning("class reminder send failed: %s", exc)
            for assignment, interval_kind in await notification_service.due_deadline_reminders(session, user, now_utc):
                try:
                    await bot.send_message(
                        user.telegram_id,
                        build_deadline_message(assignment, interval_kind, user.timezone),
                    )
                except Exception as exc:
                    log.warning("deadline reminder send failed: %s", exc)


async def sync_all(bot) -> None:
    async with get_session_factory()() as session:
        result = await session.execute(select(EClassAccount).where(EClassAccount.is_active.is_(True)))
        for account in result.scalars():
            try:
                outcome = await sync_service.sync_user(session, account)
                log.info("sync for account %s: courses=%s tt=%s assign=%s quiz=%s newscores=%s expired=%s err=%s",
                         account.id, outcome.courses_ok, outcome.timetable_ok,
                         outcome.assignments_ok, outcome.quizzes_ok, len(outcome.new_scores),
                         outcome.session_expired, outcome.error)
                if outcome.session_expired:
                    u = (await session.execute(select(User).where(User.id == account.user_id))).scalar_one()
                    try:
                        await bot.send_message(
                            u.telegram_id,
                            "🔐 Your E-Class session has expired.\n"
                            "Please reconnect your E-Class account with /start.",
                        )
                    except Exception as exc:
                        log.warning("session-expired notice failed: %s", exc)
                if outcome.new_scores:
                    u = (await session.execute(select(User).where(User.id == account.user_id))).scalar_one()
                    await notification_service.send_score_notifications(session, bot, u, outcome.new_scores)
            except Exception as exc:
                log.exception("sync failed: %s", exc)


async def send_weekly_attendance_reports(bot) -> None:
    """Saturday 21:00 (configured timezone): one attendance report per active user."""
    from datetime import timedelta

    from app.services import attendance_service, notification_service

    now_utc = datetime.now(__import__("datetime").timezone.utc)
    async with get_session_factory()() as session:
        result = await session.execute(
            select(EClassAccount).where(EClassAccount.is_active.is_(True))
        )
        for account in result.scalars():
            u = (await session.execute(select(User).where(User.id == account.user_id))).scalar_one_or_none()
            if u is None:
                continue
            tz = ZoneInfo(u.timezone or get_settings().default_timezone)
            local = now_utc.astimezone(tz)
            week_start = (local.date() - timedelta(days=local.weekday())).isoformat()
            if not await notification_service.due_weekly_attendance(session, u.id, week_start):
                continue  # already reported this week (restart / re-run safe)
            rows = await attendance_service.get_persisted(session, u.id)
            if not rows:
                continue  # no successful attendance sync ever: nothing to report
            stale = attendance_service.attendance_is_stale(account, rows)
            await notification_service.mark_weekly_attendance_sent(session, u.id, week_start)
            try:
                await bot.send_message(
                    u.telegram_id,
                    attendance_service.build_weekly_report_from_rows(rows, stale, u.timezone),
                )
            except Exception as exc:
                log.warning("weekly attendance send failed: %s", exc)


def build_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_notifications, "interval", minutes=1, args=[bot], id="notifications")
    scheduler.add_job(sync_all, "interval", minutes=30, args=[bot], id="sync")
    scheduler.add_job(
        send_weekly_attendance_reports,
        "cron",
        day_of_week="sat",
        hour=21,
        minute=0,
        timezone=ZoneInfo(get_settings().default_timezone),
        args=[bot],
        id="weekly_attendance",
    )
    return scheduler
