from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from app.bot.keyboards.main_menu import main_menu
from app.bot.states.login import LoginStates
from app.database.database import get_session_factory
from app.database.models.eclass_account import EClassAccount
from app.database.repositories.allowed_student_id_repository import AllowedStudentIDRepository
from app.database.repositories.user_repository import UserRepository
from app.eclass.client import EClassAuthError, EClassUnavailableError
from app.eclass.web_client import EClassWebClient
from app.services.validation import (
    INVALID_STUDENT_ID_MSG,
    NOT_ALLOWED_STUDENT_ID_MSG,
    normalize_student_id,
)

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(message.from_user.id, message.from_user.username)
        account = (
            await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))
        ).scalar_one_or_none()

    if account and account.is_active:
        last = account.last_sync.strftime("%B %d, %H:%M") if account.last_sync else "never"
        await message.answer(
            f"👋 Welcome back, {message.from_user.first_name or 'student'}!\n\n"
            "✅ E-Class connected\n"
            f"🔄 Last synchronized: {last}\n",
            reply_markup=main_menu(),
        )
        return

    await state.set_state(LoginStates.waiting_student_id)
    await message.answer(
        "🎓 Welcome to Uni Assistant!\n\n"
        "Connect your E-Class account to get your university schedule, "
        "assignment deadlines, and automatic class reminders.\n\n"
        "First, enter your Student ID (your E-Class username, e.g. u2410037):"
    )


@router.message(LoginStates.waiting_student_id)
async def got_student_id(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    student_id = normalize_student_id(message.text)
    if student_id is None:
        # stay in waiting_student_id — nothing is stored, user may retry
        await message.answer(INVALID_STUDENT_ID_MSG)
        return
    async with get_session_factory()() as session:
        allowed = await AllowedStudentIDRepository(session).is_allowed(student_id)
    if not allowed:
        # stay in waiting_student_id — user may retry with an allowed ID
        await message.answer(NOT_ALLOWED_STUDENT_ID_MSG)
        return
    await state.update_data(student_id=student_id)
    await state.set_state(LoginStates.waiting_password)
    await message.answer(
        "Now enter your E-Class password.\n"
        "⚠️ Your message will be deleted immediately after processing."
    )


@router.message(LoginStates.waiting_password)
async def got_password(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return
    password = message.text
    data = await state.get_data()
    student_id: str = data["student_id"]

    try:
        await message.delete()
    except Exception:
        pass

    status = await message.answer("⏳ Checking your credentials with E-Class…")
    client = EClassWebClient()
    try:
        await client.login(student_id, password)
    except EClassAuthError:
        # Real E-Class rejected the credentials: store NOTHING, keep nothing,
        # let the user retry the password (or /start to change the ID).
        await state.set_state(LoginStates.waiting_password)
        await status.edit_text(
            "❌ E-Class authentication failed.\n\n"
            "Please check your Student ID and password and try again."
        )
        del password
        return
    except EClassUnavailableError:
        await state.clear()
        await status.edit_text("⚠️ E-Class is currently unavailable. Try /start again later.")
        del password
        return

    # Authentication succeeded against the real E-Class. The password's job is
    # done: persist ONLY the authenticated session (encrypted), never the password.
    session_cookies = client.session_data()
    del password  # drop plaintext ASAP
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_or_create(message.from_user.id, message.from_user.username)
        existing = (
            await session.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))
        ).scalar_one_or_none()
        if existing is None:
            existing = EClassAccount(user_id=user.id, username=student_id)
            session.add(existing)
        else:
            existing.username = student_id
            existing.is_active = True
        existing.session_data = session_cookies
        await session.commit()

    await state.set_state(LoginStates.waiting_group)
    await status.edit_text(
        "✅ Account created successfully!\n\n"
        "Now enter your full study group name\n(for example: ICE-24-01):"
    )


@router.message(LoginStates.waiting_group)
async def got_group(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return
    group = message.text.strip()
    from app.edupage.web_client import EduPageWebClient

    canonical = await EduPageWebClient().resolve_group_name(group)
    if canonical is None:
        await message.answer(
            "❌ I couldn't find that study group.\n\n"
            "Please enter the complete group name, for example:\nICE-24-01"
        )
        return
    async with get_session_factory()() as session:
        from app.services.group_service import get_or_create_group

        user = await UserRepository(session).get_or_create(message.from_user.id, message.from_user.username)
        g = await get_or_create_group(session, canonical)
        user.group_id = g.id
        await session.commit()
    await state.clear()
    await message.answer(
        "✅ Account connected successfully!\n\n"
        f"🎓 Group: {canonical}\n\n"
        "Your university assistant is now ready.\n\n"
        "I will automatically track:\n"
        "📅 Timetable\n⏰ Class reminders\n📝 Homework\n🧪 Quizzes\n📌 Assignments\n⏳ Deadlines\n📊 Scores\n\n"
        "Running initial synchronization — use 🔄 Sync anytime.",
        reply_markup=main_menu(),
    )
    # initial sync happens via scheduler 'sync_all' job; trigger a lightweight notice
    await message.answer("🔄 Synchronization will run automatically every 30 minutes. Use 🔄 Sync for an immediate run.")


HELP_TEXT = (
    "🎓 ClassMate AI — university academic assistant\n\n"
    "Available commands:\n\n"
    "/start — connect/login your E-Class account\n"
    "/today — today's lessons\n"
    "/tomorrow — tomorrow's lessons\n"
    "/week — weekly timetable\n"
    "/next — next upcoming lesson\n"
    "/assignments — current assignments\n"
    "/quizzes — current quizzes\n"
    "/deadlines — upcoming deadlines\n"
    "/scores — score/grade information\n"
    "/attendance — attendance for your courses\n"
    "/sync — manually synchronize academic data\n"
    "/settings — manage account/settings\n"
    "/help — show this help message\n\n"
    "🔐 Password security\n"
    "Your E-Class password is not stored in ClassMate AI. It is used to "
    "authenticate with E-Class, and your password message is deleted. The bot "
    "keeps only the encrypted E-Class session needed to access your academic "
    "data without asking for your password again.\n\n"
    "🔄 Session\n"
    "ClassMate AI periodically synchronizes with E-Class to keep the "
    "authenticated session active. The current full synchronization runs every "
    "30 minutes. If the E-Class session eventually expires or becomes invalid, "
    "the bot will notify you and ask you to reconnect.\n\n"
    "⏰ Deadline reminders\n"
    "Assignments and quizzes get one reminder 4 hours before the deadline and "
    "one reminder 1 hour before it. Each reminder is sent only once per item, "
    "and no reminder is sent for work you have already submitted."
)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)
