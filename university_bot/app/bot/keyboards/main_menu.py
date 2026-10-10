from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Button labels — single source of truth, shared by keyboard and handlers.
BTN_TODAY = "📅 Today"
BTN_TOMORROW = "📆 Tomorrow"
BTN_WEEK = "🗓 This Week"
BTN_NEXT = "➡️ Next Class"
BTN_ASSIGNMENTS = "📝 Assignments"
BTN_SYNC = "🔄 Sync"
BTN_SETTINGS = "⚙️ Settings"
BTN_QUIZZES = "🧪 Quizzes"
BTN_DEADLINES = "📌 Deadlines"
BTN_SCORES = "📊 Scores"
BTN_ATTENDANCE = "🗓 Attendance"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_NEXT)],
            [KeyboardButton(text=BTN_ASSIGNMENTS), KeyboardButton(text=BTN_QUIZZES)],
            [KeyboardButton(text=BTN_DEADLINES), KeyboardButton(text=BTN_SCORES)],
            [KeyboardButton(text=BTN_ATTENDANCE), KeyboardButton(text=BTN_SYNC)],
            [KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
    )
