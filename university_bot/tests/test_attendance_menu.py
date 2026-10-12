"""Attendance menu: single-screen subject list, ownership, and report flow.

These cover the parts that must hold regardless of what E-Class returns: the
subject keyboard is built from live data, and ownership is checked before any
database lookup happens.
"""

from __future__ import annotations

import pytest

import app.bot.handlers.attendance as att
from app.bot.handlers.attendance import (
    CB_BACK,
    CB_OVERALL,
    CB_SUBJ,
    MAX_SUBJECTS,
    OVERALL_BUTTON,
    _back_kb,
    _subjects_kb,
    subject_label,
)

TG_ID = 555000111
OTHER_ID = 999000222

# Real course names as returned by E-Class (with the trailing course code).
REAL_COURSES = [
    ("2528", "Electronic Circuit[202602-ICE2080-001]"),
    ("2533", "Signals and Systems[202602-ICE3110-001]"),
    ("2581", "Engineering Communications[202602-NTS3020-001]"),
    ("2583", "The New History of Uzbekistan, Philosophy, Basics of Spirituality and Jurisprudence-2[202602-NTS3032-001]"),
    ("2603", "Operating System[202602-SOC3010-001]"),
    ("2607", "Database[202602-SOC3020-001]"),
    ("2611", "Computer Algorithm[202602-SOC3030-001]"),
]


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.markup = None

    async def answer(self, text: str, **kw):
        self.answers.append(text)

    async def edit_text(self, text: str, reply_markup=None, **kw):
        self.edits.append(text)
        self.text = text
        if reply_markup is not None:
            self.markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.markup = reply_markup


class FakeUser:
    def __init__(self, uid: int = TG_ID):
        self.id = uid
        self.username = "student"
        self.first_name = "Student"


class FakeCallback:
    def __init__(self, data: str, uid: int = TG_ID):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage()
        self.answered = ""
        self.alert = False

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answered = text
        self.alert = show_alert


@pytest.fixture()
def fake_db(monkeypatch):
    """Patch the DB session factory used by the handler."""

    class FakeResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class FakeSession:
        def __init__(self, value=1):
            self.value = value

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, q):
            return FakeResult(self.value)

        async def commit(self):
            pass

    class FakeRepo:
        def __init__(self, session):
            pass

        async def get_or_create(self, tg_id, username):
            return FakeUser()

    monkeypatch.setattr(att, "UserRepository", FakeRepo)
    return lambda value=1: FakeSession(value)


def _use_courses(monkeypatch, courses=REAL_COURSES):
    async def fake_list(session, uid):
        return list(courses)

    monkeypatch.setattr(att.svc, "list_subjects", fake_list)


# ---------------- single-screen vertical list ----------------


def test_all_subjects_appear_on_one_screen():
    kb = _subjects_kb(REAL_COURSES, TG_ID)
    subject_rows = [r for r in kb.inline_keyboard if r[0].callback_data.startswith(CB_SUBJ)]
    assert len(subject_rows) == len(REAL_COURSES) == 7


def test_one_subject_per_row():
    kb = _subjects_kb(REAL_COURSES, TG_ID)
    for row in kb.inline_keyboard:
        assert len(row) == 1, "each button must occupy its own row"


def test_no_pagination_controls_remain():
    """The intermediate menu and page buttons are gone."""
    kb = _subjects_kb(REAL_COURSES, TG_ID)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert not any("Next" in t or "Previous" in t or "Page" in t for t in labels)


def test_overall_report_button_is_last():
    kb = _subjects_kb(REAL_COURSES, TG_ID)
    last = kb.inline_keyboard[-1][0]
    assert last.text == OVERALL_BUTTON
    assert last.callback_data == f"{CB_OVERALL}:{TG_ID}"


def test_labels_have_no_course_code():
    kb = _subjects_kb(REAL_COURSES, TG_ID)
    for row in kb.inline_keyboard[:-1]:
        assert "[" not in row[0].text and "]" not in row[0].text


def test_labels_strip_html_markup():
    """Telegram renders button text literally, so HTML must be removed."""
    kb = _subjects_kb([("1", "<b>Math</b>[202602-A-001]")], TG_ID)
    assert kb.inline_keyboard[0][0].text == "📚 Math"


def test_long_name_is_truncated_but_still_one_row():
    long_name = "X" * 300 + "[202602-VERY-LONG-CODE-000]"
    kb = _subjects_kb([("1", long_name)], TG_ID)
    text = kb.inline_keyboard[0][0].text
    assert len(kb.inline_keyboard[0]) == 1
    assert len(text) <= 45


def test_label_never_blank():
    assert subject_label("") != ""
    assert subject_label("[202602-A-001]").strip() != ""


def test_duplicate_names_remain_distinct_via_callback_id():
    dup = [("1", "Math[202602-A-001]"), ("2", "Math[202602-B-002]")]
    kb = _subjects_kb(dup, TG_ID)
    cbs = [row[0].callback_data for row in kb.inline_keyboard[:2]]
    assert cbs == [f"{CB_SUBJ}:1:{TG_ID}", f"{CB_SUBJ}:2:{TG_ID}"]


def test_course_id_maps_to_correct_button():
    kb = _subjects_kb(REAL_COURSES, TG_ID)
    mapping = {row[0].text: row[0].callback_data for row in kb.inline_keyboard}
    assert mapping["📚 Electronic Circuit"] == f"{CB_SUBJ}:2528:{TG_ID}"
    assert mapping["📚 Operating System"] == f"{CB_SUBJ}:2603:{TG_ID}"


def test_callback_data_within_telegram_limit():
    kb = _subjects_kb([(str(10**12), "N" * 200) for _ in range(3)], TG_ID)
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64


def test_empty_course_list_still_renders():
    kb = _subjects_kb([], TG_ID)
    assert kb.inline_keyboard[-1][0].callback_data == f"{CB_OVERALL}:{TG_ID}"


async def test_oversized_list_is_truncated_not_hidden_silently(monkeypatch):
    """Above Telegram's keyboard limit the excess is stated, never silently dropped."""
    many = [(str(i), f"Course {i}") for i in range(MAX_SUBJECTS + 15)]
    _use_courses(monkeypatch, many)
    call = FakeCallback(f"{CB_BACK}:{TG_ID}")

    class Svc:
        async def __aenter__(self):
            return call.message

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(att, "get_session_factory", lambda: Svc)
    await att._send_subjects(call.message, 7, TG_ID)

    subject_rows = [
        r for r in call.message.markup.inline_keyboard if r[0].callback_data.startswith(CB_SUBJ)
    ]
    assert len(subject_rows) == MAX_SUBJECTS
    assert "limit" in call.message.text.lower()


# ---------------- ownership ----------------


async def test_foreign_button_is_rejected(fake_db):
    call = FakeCallback(f"{CB_SUBJ}:2581:{TG_ID}", uid=OTHER_ID)
    assert await att._user_id(call) is None
    assert call.alert is True
    assert "another account" in call.answered


async def test_owner_button_resolves(fake_db):
    call = FakeCallback(f"{CB_SUBJ}:2581:{TG_ID}", uid=TG_ID)
    assert await att._user_id(call) is not None
    assert call.alert is False


async def test_button_without_owner_suffix_is_refused(fake_db):
    """A numeric course id must never be mistaken for an owner id."""
    call = FakeCallback("att:subj:42")
    assert await att._user_id(call) is None
    assert call.alert is True


async def test_stale_catchall_always_answers():
    call = FakeCallback("att:by:555000111")
    await att.stale_callback(call)
    assert "outdated" in call.answered


# ---------------- navigation ----------------


async def test_back_button_returns_to_full_subject_list(fake_db, monkeypatch):
    _use_courses(monkeypatch)
    call = FakeCallback(f"{CB_BACK}:{TG_ID}")

    class Svc:
        async def __aenter__(self):
            return call.message

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(att, "get_session_factory", lambda: Svc)
    await att.back_to_subjects(call)

    labels = [b.text for row in call.message.markup.inline_keyboard for b in row]
    assert any("Electronic Circuit" in t for t in labels)
    assert any("Operating System" in t for t in labels)


def test_back_keyboard_targets_back_callback():
    kb = _back_kb(TG_ID)
    assert kb.inline_keyboard[0][0].callback_data == f"{CB_BACK}:{TG_ID}"


async def test_subject_callback_shows_minimal_report(fake_db, monkeypatch):
    async def fake_report(session, uid, course_id):
        return "📚 <b>Operating System</b>\n\n❌ Absences: 1"

    monkeypatch.setattr(att.svc, "subject_report_text", fake_report)

    class Svc:
        async def __aenter__(self):
            return FakeMessage()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(att, "get_session_factory", lambda: Svc)
    call = FakeCallback(f"{CB_SUBJ}:2603:{TG_ID}")
    await att.show_subject(call)
    assert "Operating System" in call.message.text
    assert "Absences: 1" in call.message.text


async def test_overall_callback_renders_report(fake_db, monkeypatch):
    async def fake_overall(session, uid):
        return "📊 Overall\n\n<b>Total absences: 4</b>"

    monkeypatch.setattr(att.svc, "manual_overall_text", fake_overall)

    class Svc:
        async def __aenter__(self):
            return FakeMessage()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(att, "get_session_factory", lambda: Svc)
    call = FakeCallback(f"{CB_OVERALL}:{TG_ID}")
    await att.overall(call)
    assert "Total absences: 4" in call.message.text


async def test_subject_failure_does_not_show_zero(fake_db, monkeypatch):
    async def boom(session, uid, course_id):
        raise RuntimeError("E-Class unreachable")

    monkeypatch.setattr(att.svc, "subject_report_text", boom)

    class Svc:
        async def __aenter__(self):
            return FakeMessage()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(att, "get_session_factory", lambda: Svc)
    call = FakeCallback(f"{CB_SUBJ}:2603:{TG_ID}")
    await att.show_subject(call)
    assert "unavailable" in call.message.text.lower()
    assert "Absences: 0" not in call.message.text


def test_router_registers_attendance_handlers():
    assert "cmd_attendance" in {h.callback.__name__ for h in att.router.message.handlers}
    assert att.router.callback_query.handlers