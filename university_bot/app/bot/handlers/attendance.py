"""/attendance command: course picker + per-course attendance details."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.database.database import get_session_factory
from app.database.models.eclass_account import EClassAccount
from app.database.repositories.user_repository import UserRepository
from app.services import attendance_service
from app.bot.keyboards.main_menu import BTN_ATTENDANCE

router = Router()

def courses_kb_from_rows(rows) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=r.course_name, callback_data=f"att:course:{r.course_external_id}")]
        for r in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="att:list")]]
    )


async def _get_user_and_account(telegram_id: int, username: str | None):
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(telegram_id, username)
        account = (
            await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))
        ).scalar_one_or_none()
        return user.id, user.timezone, account


@router.message(Command("attendance"))
@router.message(F.text == BTN_ATTENDANCE)
async def cmd_attendance(message: Message) -> None:
    if message.from_user is None:
        return
    uid, tz, account = await _get_user_and_account(message.from_user.id, message.from_user.username)
    if account is None:
        await message.answer("🔐 No E-Class account connected. Send /start to connect.")
        return
    async with get_session_factory()() as session:
        rows = await attendance_service.get_persisted(session, uid)
        stale = attendance_service.attendance_is_stale(account, rows)
    if not rows:
        await message.answer("📚 No attendance data yet. Run 🔄 Sync first.")
        return
    prefix = attendance_service.STALE_WARNING + "\n\n" if stale else ""
    await message.answer(
        prefix + "📊 Attendance\n\nChoose a course:",
        reply_markup=courses_kb_from_rows(rows),
    )


@router.callback_query(F.data == "att:list")
async def att_back_to_courses(call: CallbackQuery) -> None:
    uid, tz, account = await _get_user_and_account(call.from_user.id, call.from_user.username)
    async with get_session_factory()() as session:
        rows = await attendance_service.get_persisted(session, uid)
        stale = attendance_service.attendance_is_stale(account, rows)
    await call.message.edit_text(
        (attendance_service.STALE_WARNING + "\n\n" if stale else "")
        + "📊 Attendance\n\nChoose a course:",
        reply_markup=courses_kb_from_rows(rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("att:course:"))
async def att_course(call: CallbackQuery) -> None:
    external_id = call.data.split(":")[-1]
    uid, tz, account = await _get_user_and_account(call.from_user.id, call.from_user.username)
    async with get_session_factory()() as session:
        rows = await attendance_service.get_persisted(session, uid)
        stale = attendance_service.attendance_is_stale(account, rows)
    match = next((r for r in rows if r.course_external_id == external_id), None)
    if match is None:
        await call.message.edit_text("ℹ️ Attendance data not found for this course.", reply_markup=_back_kb())
    else:
        text = attendance_service.format_attendance_row(match, tz)
        if stale:
            text = attendance_service.STALE_WARNING + "\n\n" + text
        await call.message.edit_text(text, reply_markup=_back_kb())
    await call.answer()
