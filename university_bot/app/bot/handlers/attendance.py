"""Attendance menu: full subject list -> minimal per-subject absence count.

`/attendance` (or the 🗒 Attendance menu button) lists EVERY subject on one
screen, one per row, with an 📊 Overall report button at the bottom. There is no
intermediate menu: subjects are one tap away.

Every callback carries the Telegram id it was issued for as its FINAL segment,
so ownership is checked positionally and unambiguously. A stale button tapped by
another account is rejected before any lookup, and one whose owner cannot be
identified is refused rather than guessed at.
"""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.keyboards.main_menu import BTN_ATTENDANCE
from app.database.database import get_session_factory
from app.database.models.eclass_account import EClassAccount
from app.database.repositories.user_repository import UserRepository
from app.services import attendance_service as svc

log = logging.getLogger(__name__)

router = Router()

# Telegram allows up to 100 buttons per inline keyboard; one row per subject
# stays comfortable on a phone well below that, so all subjects fit on screen.
MAX_SUBJECTS = 90
# Buttons render poorly past ~64 chars, so trim the label (the report and the
# callback keep the full name / stable course id).
MAX_LABEL = 40

CB_SUBJ = "att:subj"        # att:subj:<course_id>:<telegram_id>
CB_OVERALL = "att:all"       # att:all:<telegram_id>
CB_BACK = "att:back"         # att:back:<telegram_id>

LOADING = "⏳ Loading attendance…"
SUBJECTS_TEXT = "📚 <b>Attendance</b>\n\nSelect a subject:"
OVERALL_BUTTON = "📊 Overall report"


def subject_label(name: str) -> str:
    """Readable button label: no course code, no numbering, no markup.

    Telegram renders button text literally, so any HTML coming from E-Class must
    be stripped rather than escaped.
    """
    clean = re.sub(r"<[^>]+>", "", svc.display_name(name))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:MAX_LABEL].strip() or "Course"


def _subjects_kb(courses: list[tuple[str, str]], tg_id: int) -> InlineKeyboardMarkup:
    """Every subject on one screen, one per row, plus Overall report."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📚 {subject_label(name)}",
                callback_data=f"{CB_SUBJ}:{cid}:{tg_id}",
            )
        ]
        for cid, name in courses
    ]
    rows.append(
        [InlineKeyboardButton(text=OVERALL_BUTTON, callback_data=f"{CB_OVERALL}:{tg_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_kb(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"{CB_BACK}:{tg_id}")]
        ]
    )


async def _user_id(call: CallbackQuery) -> int | None:
    """Resolve the internal user id after verifying the button's owner.

    The owner must be the final segment of the callback data and must match the
    tapping user. Anything else is refused: an unverifiable button must never
    reach a data lookup, because that could render another student's records.
    """
    parts = (call.data or "").split(":")
    if not parts[-1].isdigit() or int(parts[-1]) != call.from_user.id:
        await call.answer("This menu belongs to another account.", show_alert=True)
        log.warning("attendance: rejected callback %r from user %s", call.data, call.from_user.id)
        return None
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(
            call.from_user.id, call.from_user.username
        )
    return user.id


async def _send_subjects(target, uid: int, tg_id: int) -> None:
    """Render the full subject list (used by /attendance and by Back)."""
    async with get_session_factory()() as session:
        courses = await svc.list_subjects(session, uid)

    if not courses:
        await target.edit_text(
            "⚠️ No subjects could be retrieved from E-Class.",
            reply_markup=_back_kb(tg_id),
        )
        return

    if len(courses) > MAX_SUBJECTS:
        # Explicit limit handling - never silently hide courses.
        log.warning("attendance: %d courses exceeds keyboard limit for user %s",
                    len(courses), uid)
        courses = courses[:MAX_SUBJECTS]
        await target.edit_text(
            f"⚠️ {len(courses)} subjects shown (Telegram's limit is {MAX_SUBJECTS}).",
            reply_markup=_subjects_kb(courses, tg_id),
        )
        return

    await target.edit_text(
        SUBJECTS_TEXT, reply_markup=_subjects_kb(courses, tg_id)
    )


def _no_account_text() -> str:
    return "🔐 No E-Class account connected.\nSend /start to connect your account first."


@router.message(Command("attendance"))
@router.message(F.text == BTN_ATTENDANCE)
async def cmd_attendance(message: Message) -> None:
    if message.from_user is None:
        return
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(
            message.from_user.id, message.from_user.username
        )
        connected = (
            await session.execute(
                select(EClassAccount.id).where(
                    EClassAccount.user_id == user.id, EClassAccount.is_active.is_(True)
                )
            )
        ).scalar_one_or_none() is not None
        if not connected:
            await message.answer(_no_account_text())
            return
        courses = await svc.list_subjects(session, user.id)

    tg_id = message.from_user.id
    if not courses:
        await message.answer("⚠️ No subjects could be retrieved from E-Class.")
        return
    shown = courses[:MAX_SUBJECTS]
    header = SUBJECTS_TEXT
    if len(courses) > MAX_SUBJECTS:
        header = f"⚠️ Showing {MAX_SUBJECTS} of {len(courses)} subjects (Telegram limit)."
    await message.answer(header, reply_markup=_subjects_kb(shown, tg_id))


@router.callback_query(F.data.startswith(f"{CB_SUBJ}:"))
async def show_subject(call: CallbackQuery) -> None:
    uid = await _user_id(call)
    if uid is None:
        return
    parts = (call.data or "").split(":")
    if len(parts) < 3 or not parts[2]:
        await call.answer("Stale button — reopen /attendance.", show_alert=True)
        return
    course_id = parts[2]
    await call.answer()
    await call.message.edit_text(LOADING)
    try:
        async with get_session_factory()() as session:
            text = await svc.subject_report_text(session, uid, course_id)
    except Exception as exc:  # noqa: BLE001 - never leave a stuck spinner
        log.warning("attendance: subject %s failed for user %s: %s", course_id, uid, exc)
        text = "⚠️ Attendance data unavailable"
    await call.message.edit_text(
        text, reply_markup=_back_kb(call.from_user.id), disable_web_page_preview=True
    )


@router.callback_query(F.data.startswith(f"{CB_OVERALL}:"))
async def overall(call: CallbackQuery) -> None:
    uid = await _user_id(call)
    if uid is None:
        return
    await call.answer()
    await call.message.edit_text(LOADING)
    try:
        async with get_session_factory()() as session:
            text = await svc.manual_overall_text(session, uid)
    except Exception as exc:  # noqa: BLE001
        log.warning("attendance: overall report failed for user %s: %s", uid, exc)
        text = "⚠️ Attendance data unavailable"
    await call.message.edit_text(
        text, reply_markup=_back_kb(call.from_user.id), disable_web_page_preview=True
    )


@router.callback_query(F.data.startswith(f"{CB_BACK}:"))
async def back_to_subjects(call: CallbackQuery) -> None:
    uid = await _user_id(call)
    if uid is None:
        return
    await call.answer()
    try:
        await _send_subjects(call.message, uid, call.from_user.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("attendance: back navigation failed for user %s: %s", uid, exc)
        await call.message.edit_text("⚠️ Could not load subjects.")


@router.callback_query(F.data.startswith("att:"))
async def stale_callback(call: CallbackQuery) -> None:
    """Catch-all so buttons from an older layout never hang."""
    await call.answer("This button is outdated — reopen /attendance.", show_alert=True)