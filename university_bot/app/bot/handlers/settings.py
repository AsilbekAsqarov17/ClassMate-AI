"""Settings: view + change student ID, E-Class password, study group, notifications."""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.database.database import get_session_factory
from app.database.models.eclass_account import EClassAccount
from app.database.models.user import User
from app.database.repositories.user_repository import UserRepository
from app.eclass.client import EClassAuthError, EClassUnavailableError
from app.eclass.web_client import EClassWebClient
from app.services.validation import INVALID_STUDENT_ID_MSG, normalize_student_id

router = Router()


class SettingsStates(StatesGroup):
    waiting_new_student_id = State()
    waiting_id_password = State()       # one-time password for a NEW Student ID
    waiting_reconnect_password = State()  # one-time password to renew the session
    waiting_new_group = State()


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Change Student ID", callback_data="set:student_id")],
            [InlineKeyboardButton(text="🔐 Reconnect E-Class", callback_data="set:reconnect")],
            [InlineKeyboardButton(text="✏️ Change Study group", callback_data="set:group")],
            [InlineKeyboardButton(text="🔔 Notification settings", callback_data="set:notif")],
        ]
    )


def notif_kb(user: User) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📅 Daily timetable: {'ON' if user.daily_timetable_notifications else 'OFF'}", callback_data="set:notif:toggle_daily")],
            [InlineKeyboardButton(text=f"⏰ Class reminders: {'ON' if user.class_notifications else 'OFF'}", callback_data="set:notif:toggle_class")],
            [InlineKeyboardButton(text=f"🔔 Deadline reminders: {'ON' if user.deadline_notifications else 'OFF'}", callback_data="set:notif:toggle_deadline")],
            [InlineKeyboardButton(text=f"Reminder time: {user.reminder_minutes} min", callback_data="set:notif:cycle_minutes")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="set:back")],
        ]
    )


def settings_text(user: User, account: EClassAccount | None) -> str:
    return (
        "⚙️ Settings\n\n"
        f"🎓 Student ID: {account.username if account else '-'}\n"
        f"👥 Study group: {user.group.name if user.group else '-'}\n\n"
        f"📅 Daily timetable: {'ON' if user.daily_timetable_notifications else 'OFF'}\n"
        f"⏰ Class reminders: {'ON' if user.class_notifications else 'OFF'}\n"
        f"⏰ Reminder time: {user.reminder_minutes} minutes\n"
        f"🔔 Deadline reminders: {'ON' if user.deadline_notifications else 'OFF'}"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if message.from_user is None:
        return
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(message.from_user.id, message.from_user.username)
        account = (
            await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))
        ).scalar_one_or_none()
        await message.answer(settings_text(user, account), reply_markup=settings_kb())


def _settings_button_text() -> str:
    from app.bot.keyboards.main_menu import BTN_SETTINGS

    return BTN_SETTINGS


@router.message(F.text == _settings_button_text())
async def menu_settings(message: Message) -> None:
    await cmd_settings(message)


@router.callback_query(F.data == "set:back")
async def back_to_settings(call: CallbackQuery) -> None:
    async with get_session_factory()() as session:
        user = (await session.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one()
        account = (await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))).scalar_one_or_none()
        await call.message.edit_text(settings_text(user, account), reply_markup=settings_kb())
    await call.answer()


@router.callback_query(F.data == "set:notif")
async def notif_settings(call: CallbackQuery) -> None:
    async with get_session_factory()() as session:
        user = (await session.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one()
        await call.message.edit_text(
            "🔔 Notification settings\n\nToggle options below.",
            reply_markup=notif_kb(user),
        )
    await call.answer()


@router.callback_query(F.data.startswith("set:notif:"))
async def notif_toggle(call: CallbackQuery) -> None:
    action = call.data.split(":")[-1]
    async with get_session_factory()() as session:
        user = (await session.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one()
        if action == "toggle_class":
            user.class_notifications = not user.class_notifications
        elif action == "toggle_daily":
            user.daily_timetable_notifications = not user.daily_timetable_notifications
        elif action == "toggle_deadline":
            user.deadline_notifications = not user.deadline_notifications
        elif action == "cycle_minutes":
            user.reminder_minutes = {15: 30, 30: 60}.get(user.reminder_minutes, 15)
        await session.commit()
        await session.refresh(user)
        await call.message.edit_text(
            "🔔 Notification settings\n\nToggle options below.",
            reply_markup=notif_kb(user),
        )
    await call.answer()


# ---------- Change Student ID ----------

@router.callback_query(F.data == "set:student_id")
async def ask_student_id(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_new_student_id)
    await call.message.answer("Please enter your new Student ID (E-Class username):")
    await call.answer()


@router.message(SettingsStates.waiting_new_student_id)
async def got_new_student_id(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return
    new_id = normalize_student_id(message.text)
    if new_id is None:
        # stay in waiting_new_student_id — nothing is stored, user may retry
        await message.answer(INVALID_STUDENT_ID_MSG)
        return
    # The password is never stored, so switching to a different Student ID
    # requires a fresh one-time authentication for that ID.
    await state.update_data(new_student_id=new_id)
    await state.set_state(SettingsStates.waiting_id_password)
    await message.answer(
        f"🎓 New Student ID: {new_id}\n\n"
        "Enter your E-Class password once to authenticate this account.\n"
        "🔒 Your message will be deleted immediately after processing."
    )


@router.message(SettingsStates.waiting_id_password)
async def got_id_password(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return
    password = message.text
    data = await state.get_data()
    new_id: str = data["new_student_id"]
    try:
        await message.delete()
    except Exception:
        pass
    status = await message.answer("⏳ Verifying with E-Class…")
    try:
        client = EClassWebClient()
        await client.login(new_id, password)
    except EClassAuthError:
        del password
        await state.clear()
        await status.edit_text("❌ Authentication failed.\nYour previous E-Class session was kept.")
        return
    except EClassUnavailableError:
        del password
        await state.clear()
        await status.edit_text("⚠️ E-Class is currently unavailable. Try again later; your saved session is unchanged.")
        return
    session_cookies = client.session_data()
    del password  # drop plaintext ASAP — it is never stored
    async with get_session_factory()() as session:
        user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
        account = (await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))).scalar_one()
        # replaced ONLY after a verified login
        account.username = new_id
        account.session_data = session_cookies
        account.is_active = True
        await session.commit()
    await state.clear()
    await status.edit_text(f"✅ Student ID changed successfully.\n\n🎓 Student ID: {new_id}")


# ---------- Reconnect E-Class (renew session) ----------

@router.callback_query(F.data == "set:reconnect")
async def ask_reconnect(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_reconnect_password)
    await call.message.answer(
        "🔐 Reconnect E-Class\n\n"
        "Enter your E-Class password once to create a new session.\n"
        "🔒 Your message will be deleted immediately after processing.\n"
        "The password is never stored — only the authenticated session."
    )
    await call.answer()


@router.message(SettingsStates.waiting_reconnect_password)
async def got_reconnect_password(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.text or not message.from_user:
        return
    password = message.text
    try:
        await message.delete()
    except Exception:
        pass
    status = await message.answer("⏳ Verifying with E-Class…")
    try:
        async with get_session_factory()() as session:
            user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
            account = (await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))).scalar_one()
            username = account.username
        client = EClassWebClient()
        await client.login(username, password)
    except EClassAuthError:
        del password
        await state.clear()
        await status.edit_text("❌ Authentication failed.\nYour previous E-Class session was kept.")
        return
    except EClassUnavailableError:
        del password
        await state.clear()
        await status.edit_text("⚠️ E-Class is currently unavailable. Try again later; your saved session is unchanged.")
        return
    session_cookies = client.session_data()
    del password  # drop plaintext ASAP — it is never stored
    async with get_session_factory()() as session:
        user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
        account = (await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))).scalar_one()
        # old session replaced ONLY after successful authentication
        account.session_data = session_cookies
        account.is_active = True
        await session.commit()
    await state.clear()
    await status.edit_text("✅ E-Class reconnected successfully.")


# ---------- Change Study group ----------

@router.callback_query(F.data == "set:group")
async def ask_group(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_new_group)
    await call.message.answer(
        "Please enter your full study group name, including the department/major prefix (for example: ICE-24-01)."
    )
    await call.answer()


@router.message(SettingsStates.waiting_new_group)
async def got_new_group(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return
    entered = message.text.strip()
    from app.edupage.web_client import EduPageWebClient

    canonical = await EduPageWebClient().resolve_group_name(entered)
    if canonical is None:
        await message.answer(
            "❌ I couldn't find that study group.\n\n"
            "Please enter the complete group name, for example:\nICE-24-01"
        )
        return
    async with get_session_factory()() as session:
        from app.services.group_service import get_or_create_group

        user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
        g = await get_or_create_group(session, canonical)
        user.group_id = g.id
        await session.commit()

        from app.database.models.eclass_account import EClassAccount
        from app.services.sync_service import sync_user

        account = (await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))).scalar_one_or_none()
        tt_ok = False
        if account:
            outcome = await sync_user(session, account)
            tt_ok = outcome.timetable_ok
        else:
            # no E-Class account: still sync the group timetable directly
            from datetime import datetime, timedelta, timezone

            from app.edupage.web_client import EduPageWebClient as _EC
            from app.database.models.lesson import Lesson
            from app.database.models.group import Group
            from sqlalchemy import delete

            today_ = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Tashkent")).date()
            taken = await _EC().get_timetable(g.name, today_, today_ + timedelta(days=30))
            await session.execute(delete(Lesson).where(Lesson.group_id == g.id, Lesson.lesson_date >= today_))
            for l in taken:
                session.add(Lesson(
                    group_id=g.id, external_id=l.external_id, course_name=l.course_name,
                    professor=l.professor, room=l.room, lesson_date=l.date,
                    start_time=datetime.strptime(l.start_time, "%H:%M").time(),
                    end_time=datetime.strptime(l.end_time, "%H:%M").time(),
                ))
            await session.commit()
            tt_ok = True
    await state.clear()
    await message.answer(
        "✅ Study group changed successfully.\n\n"
        f"👥 New group: {canonical}\n"
        + ("📅 Timetable synchronized." if tt_ok else "⚠️ Timetable sync will run at the next scheduled sync.")
    )
