from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards.main_menu import BTN_NEXT, BTN_TODAY, BTN_TOMORROW, BTN_WEEK
from app.database.database import get_session_factory
from app.database.repositories.user_repository import UserRepository
from app.services.schedule_service import (
    format_day,
    format_lesson,
    lessons_between,
    lessons_on,
    next_lesson,
    today,
)

router = Router()


async def _uid(message: Message) -> int | None:
    if message.from_user is None:
        return None
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(message.from_user.id, message.from_user.username)
        return user.id, user.timezone


@router.message(Command("today"))
@router.message(F.text == BTN_TODAY)
async def cmd_today(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    async with get_session_factory()() as session:
        lessons = await lessons_on(session, uid, today(tz))
        await message.answer(format_day(lessons, today(tz)))


@router.message(Command("tomorrow"))
@router.message(F.text == BTN_TOMORROW)
async def cmd_tomorrow(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    d = today(tz) + timedelta(days=1)
    async with get_session_factory()() as session:
        lessons = await lessons_on(session, uid, d)
        await message.answer(format_day(lessons, d))


@router.message(Command("week"))
@router.message(F.text == BTN_WEEK)
async def cmd_week(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    start = today(tz)
    end = start + timedelta(days=6)
    async with get_session_factory()() as session:
        lessons = await lessons_between(session, uid, start, end)
        if not lessons:
            await message.answer("🎉 No classes this week.")
            return
        by_day: dict = {}
        for l in lessons:
            by_day.setdefault(l.lesson_date, []).append(l)
        parts = []
        for d in sorted(by_day):
            parts.append(f"📅 {d:%A, %B %d}")
            for l in by_day[d]:
                parts.append(f"   {l.start_time:%H:%M} — {l.end_time:%H:%M}  {l.course_name} ({l.room or '—'})")
            parts.append("")
        await message.answer("\n".join(parts))


@router.message(Command("next"))
@router.message(F.text == BTN_NEXT)
async def cmd_next(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    async with get_session_factory()() as session:
        found = await next_lesson(session, uid, tz)
        if found is None:
            await message.answer("🎉 No upcoming classes.")
            return
        lesson, delta = found
        minutes = int(delta.total_seconds() // 60)
        await message.answer(f"➡️ Next class\n\n{format_lesson(lesson)}\n\nStarts in {minutes} minutes.")
