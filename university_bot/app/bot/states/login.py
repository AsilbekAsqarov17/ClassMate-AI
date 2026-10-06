from aiogram.fsm.state import State, StatesGroup


class LoginStates(StatesGroup):
    waiting_student_id = State()
    waiting_password = State()
    waiting_group = State()
