from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards.main_menu import (
    BTN_ASSIGNMENTS,
    BTN_DEADLINES,
    BTN_NEXT,
    BTN_QUIZZES,
    BTN_SCORES,
    BTN_SYNC,
)
from app.database.database import get_session_factory
from app.database.repositories.user_repository import UserRepository
from app.services.assignment_service import format_assignment, upcoming
from app.services.grade_service import overall_average, scores
from app.services.quiz_service import format_quiz, upcoming_quizzes

router = Router()


async def _uid(message: Message):
    if message.from_user is None:
        return None
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(message.from_user.id, message.from_user.username)
        return user.id, user.timezone


@router.message(Command("assignments"))
@router.message(F.text == BTN_ASSIGNMENTS)
async def cmd_assignments(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    async with get_session_factory()() as session:
        items = [a for a in await upcoming(session, uid) if a.kind != "quiz"]
        from sqlalchemy import select as _sel

        from app.database.models.assignment import Assignment as _A
        from app.services.academic_sync import is_submitted

        rows = (await session.execute(
            _sel(_A).where(_A.user_id == uid, _A.kind != "quiz", _A.score.is_(None))
        )).scalars().all()
        completed = [r for r in rows if r.submission_status and is_submitted(r.submission_status)]
        if not items and not completed:
            await message.answer("✅ No upcoming assignments.")
            return
        parts = ["📝 Upcoming Assignments (Not completed)\n"]
        for i, a in enumerate(items, 1):
            parts.append(f"{i}. {format_assignment(a, tz)}\n")
        if not items:
            parts.append("_none_\n")
        if completed:
            parts.append("\n✅ Completed (awaiting score)\n")
            for i, a in enumerate(completed, 1):
                parts.append(f"{i}. {format_assignment(a, tz)}\n")
        await message.answer("\n".join(parts))


@router.message(Command("quizzes"))
@router.message(F.text == BTN_QUIZZES)
async def cmd_quizzes(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    async with get_session_factory()() as session:
        items = await upcoming_quizzes(session, uid)
        from sqlalchemy import select as _sel

        from app.database.models.assignment import Assignment as _A
        from app.services.academic_sync import is_submitted

        qrows = (await session.execute(
            _sel(_A).where(_A.user_id == uid, _A.kind == "quiz", _A.score.is_(None))
        )).scalars().all()
        qcompleted = [r for r in qrows if r.submission_status and is_submitted(r.submission_status)]
        if not items and not qcompleted:
            await message.answer("✅ No upcoming quizzes.")
            return
        parts = ["🧪 Upcoming Quizzes (Not completed)\n"]
        for q in items:
            parts.append(format_quiz(q, tz) + "\n")
        if not items:
            parts.append("_none_\n")
        if qcompleted:
            parts.append("\n✅ Completed (awaiting score)\n")
            for q in qcompleted:
                parts.append(format_quiz(q, tz) + "\n")
        await message.answer("\n".join(parts))


@router.message(Command("deadlines"))
@router.message(F.text == BTN_DEADLINES)
async def cmd_deadlines(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    async with get_session_factory()() as session:
        items = await upcoming(session, uid)
        if not items:
            await message.answer("✅ No upcoming deadlines.")
            return
        parts = ["📌 Upcoming Deadlines\n"]
        for i, a in enumerate(items, 1):
            from app.services.assignment_service import to_local

            _dl = to_local(a.deadline, tz)
            dl = f"{_dl:%b} {_dl.day}, {_dl:%H:%M}" if _dl else "-"
            parts.append(f"{i}. {a.course_name}\n   {a.title}\n   ⏰ {dl}\n")
        await message.answer("\n".join(parts))


@router.message(Command("scores"))
@router.message(F.text == BTN_SCORES)
async def cmd_scores(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    async with get_session_factory()() as session:
        items = await scores(session, uid)
        if not items:
            await message.answer("📊 No scores available yet.")
            return
        by_course: dict[str, list] = {}
        for a in items:
            by_course.setdefault(a.course_name, []).append(a)
        parts = ["📊 My Scores\n"]
        for course, items2 in by_course.items():
            parts.append(f"\n📚 {course}")
            for a in items2:
                parts.append(f"  {a.title} — {a.score}/{a.max_score or '?'}")
        avg = overall_average(items)
        if avg is not None:
            parts.append(f"\n\nAverage: {avg:.1f}%")
        await message.answer("\n".join(parts))


@router.message(Command("sync"))
@router.message(F.text == BTN_SYNC)
async def cmd_sync(message: Message) -> None:
    res = await _uid(message)
    if not res:
        return
    uid, tz = res
    async with get_session_factory()() as session:
        from sqlalchemy import select

        from app.database.models.eclass_account import EClassAccount
        from app.services.sync_service import sync_user

        account = (
            await session.execute(select(EClassAccount).where(EClassAccount.user_id == uid))
        ).scalar_one_or_none()
        if account is None:
            await message.answer("🔐 No E-Class account connected. Send /start to connect.")
            return
        status = await message.answer("⏳ Synchronizing…")
        outcome = await sync_user(session, account)
        if outcome.session_expired:
            await status.edit_text(
                "🔐 Your E-Class session has expired.\n"
                "Please reconnect your E-Class account with /start."
            )
            return
        lines = []
        lines.append("✅ Courses updated" if outcome.courses_ok else "⚠️ Courses failed")
        lines.append("✅ Timetable updated" if outcome.timetable_ok else "⚠️ Timetable failed")
        lines.append("✅ Assignments updated" if outcome.assignments_ok else "⚠️ Assignments failed")
        lines.append("✅ Quizzes updated" if outcome.quizzes_ok else "⚠️ Quizzes failed")
        if outcome.error:
            lines.append(f"\n⚠️ Details: {outcome.error}\nYour previously synchronized data is still available.")
        await status.edit_text("\n".join(lines))
