"""Attendance feature: parser, E-Class client, handlers, weekly report."""

from types import SimpleNamespace

import httpx
import pytest

import app.bot.handlers.attendance as att_mod
import app.scheduler.jobs as jobs
from app.bot.keyboards.main_menu import BTN_ATTENDANCE
from app.database.models.attendance import Attendance  # noqa: F401  (table registration)
from app.database.models.course import Course
from app.database.models.eclass_account import EClassAccount
from app.database.models.notification import NotificationRecord  # noqa: F401  (table registration)
from app.database.models.user import User
from app.eclass.models import AttendanceRecord, CourseAttendance
from app.eclass.web_client import EClassWebClient, parse_attendance_html
from app.services import attendance_service, notification_service

# --------------------------- parser fixtures ---------------------------

VALID_HTML = """
<html><body>
<table class="generaltable">
<tr><th>Date</th><th>Description</th><th>Status</th></tr>
<tr><td>2026-10-07 10:00</td><td>Week 7</td><td>Present</td></tr>
<tr><td>2026-10-05 10:00</td><td>Week 6</td><td>Absent</td></tr>
<tr><td>2026-10-03 10:00</td><td>Week 5</td><td>Late</td></tr>
</table>
<p>Summary — Present: 18  Late: 1  Absent: 5  Excused: 0</p>
</body></html>
"""

ZERO_ABSENCE_HTML = """
<html><body>
<table>
<tr><td>2026-10-07 10:00</td><td>Present</td></tr>
<tr><td>2026-10-05 10:00</td><td>Present</td></tr>
</table>
<p>Present: 20  Late: 0  Absent: 0</p>
</body></html>
"""

BLANK_HTML = "<html><body><div class='no-overflow'>No data available.</div></body></html>"

# Real mod_attendance report shape: a two-column summary where the status
# label and its count live in adjacent cells, plus per-session rows.
CELL_SUMMARY_HTML = """
<html><body>
<table class="generaltable">
<tr><th>Status</th><th>Count</th></tr>
<tr><td>Present</td><td>18</td></tr>
<tr><td>Late</td><td>0</td></tr>
<tr><td>Absent</td><td>5</td></tr>
<tr><td>Excused</td><td>0</td></tr>
</table>
<table>
<tr><td>2026-10-07 10:00</td><td>Present</td></tr>
<tr><td>2026-10-05 10:00</td><td>Absent</td></tr>
</table>
</body></html>
"""

# Late = 0 but Absent > 0 — the reported failure case.
LATE_ZERO_ABSENT_HTML = """
<html><body>
<table>
<tr><td>2026-10-07 10:00</td><td>Present</td></tr>
<tr><td>2026-10-06 10:00</td><td>Absent</td></tr>
<tr><td>2026-10-05 10:00</td><td>Absent</td></tr>
</table>
<p>Summary — Present: 18  Late: 0  Absent: 5  Excused: 0</p>
</body></html>
"""

# Summary row in a single line of cells (header + values on one row).
INLINE_SUMMARY_HTML = """
<table>
<tr><td>Present</td><td>18</td><td>Late</td><td>0</td><td>Absent</td><td>5</td></tr>
</table>
"""

# "Unexcused absence" must resolve to Absent, not to a shorter alias.
UNEXCUSED_HTML = """
<table>
<tr><td>Present</td><td>18</td></tr>
<tr><td>Unexcused absence</td><td>5</td></tr>
<tr><td>Excused</td><td>0</td></tr>
</table>
"""

# Attendance IS tracked, but E-Class reports no absence figure anywhere.
# Absence must stay None — never silently become 0.
NO_ABSENCE_REPORTED_HTML = """
<html><body>
<table>
<tr><td>2026-10-07 10:00</td><td>Present</td></tr>
<tr><td>2026-10-06 10:00</td><td>Present</td></tr>
</table>
<p>Summary — Present: 20  Late: 0</p>
</body></html>
"""

# Only absence rows, no summary: counts must come from the session rows.
ABSENT_ROWS_ONLY_HTML = """
<table>
<tr><td>2026-10-06 10:00</td><td>Absent</td></tr>
<tr><td>2026-10-05 10:00</td><td>Absent</td></tr>
<tr><td>2026-10-04 10:00</td><td>Present</td></tr>
</table>
"""

COURSES_HTML = """
<tr><td>1</td><td><div><span class="label label-course">OFFLINE</span> <a href="https://eclass.example/course/view.php?id=100" class="coursefullname">Calculus</a></div></td><td class="text-center">Prof. A</td><td>29</td><td>Student</td></tr>
<tr><td>2</td><td><div><span class="label label-course">OFFLINE</span> <a href="https://eclass.example/course/view.php?id=200" class="coursefullname">Programming</a></div></td><td class="text-center">Prof. B</td><td>29</td><td>Student</td></tr>
"""

COURSE_100_HTML = '<a class="tool" href="https://eclass.example/mod/attendance/view.php?id=555">Attendance</a>'
COURSE_200_HTML = '<div>no attendance tool linked here</div>'


# --------------------------- parser tests ---------------------------


def test_parser_valid_attendance():
    counts, records = parse_attendance_html(VALID_HTML)
    assert counts == {"Present": 18, "Late": 1, "Absent": 5, "Excused": 0}
    assert len(records) == 3
    assert records[0].status == "Present"
    assert records[1].status == "Absent"


def test_parser_zero_absences_is_available():
    counts, records = parse_attendance_html(ZERO_ABSENCE_HTML)
    assert counts is not None
    assert counts["Absent"] == 0
    assert len(records) == 2


def test_parser_blank_is_not_zero():
    counts, records = parse_attendance_html(BLANK_HTML)
    assert counts is None
    assert records == []


def test_parser_cell_summary_layout():
    """Status label and count in adjacent table cells (real mod_attendance)."""
    counts, records = parse_attendance_html(CELL_SUMMARY_HTML)
    assert counts["Absent"] == 5
    assert counts["Present"] == 18
    assert counts["Late"] == 0
    assert counts["Excused"] == 0
    assert len(records) == 2


def test_parser_late_zero_with_absences():
    """Late must not be confused with Absent when Late is 0."""
    counts, _ = parse_attendance_html(LATE_ZERO_ABSENT_HTML)
    assert counts["Late"] == 0
    assert counts["Absent"] == 5
    assert counts["Present"] == 18
    assert counts["Excused"] == 0


def test_parser_inline_single_row_summary():
    counts, _ = parse_attendance_html(INLINE_SUMMARY_HTML)
    assert counts == {"Present": 18, "Late": 0, "Absent": 5}


def test_parser_unexcused_absence_maps_to_absent():
    counts, _ = parse_attendance_html(UNEXCUSED_HTML)
    assert counts["Absent"] == 5
    assert counts["Excused"] == 0
    assert counts["Present"] == 18


def test_parser_missing_absence_is_not_zero():
    """No absence figure on the page → key absent → caller shows unavailable."""
    counts, records = parse_attendance_html(NO_ABSENCE_REPORTED_HTML)
    assert counts is not None
    assert "Absent" not in counts          # NOT 0
    assert counts["Late"] == 0
    assert len(records) == 2


def test_parser_derives_absence_from_session_rows():
    counts, records = parse_attendance_html(ABSENT_ROWS_ONLY_HTML)
    assert counts["Absent"] == 2
    assert counts["Present"] == 1
    assert len(records) == 3


def test_parser_counts_do_not_leak_across_labels():
    """A number following one status must not be attributed to another."""
    html = "<table><tr><td>Absent</td><td>5</td><td>Present</td><td>18</td></tr></table>"
    counts, _ = parse_attendance_html(html)
    assert counts["Absent"] == 5
    assert counts["Present"] == 18


def test_parser_summary_only_no_rows():
    counts, records = parse_attendance_html(
        "<table><tr><td>Present: 10</td><td>Absent: 2</td></tr></table>"
    )
    assert counts == {"Present": 10, "Absent": 2}
    assert records == []


# --------------------------- web client ---------------------------


def _mock_client(handler) -> EClassWebClient:
    client = EClassWebClient(base_url="https://eclass.example")
    client._client = httpx.AsyncClient(
        base_url="https://eclass.example",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    return client


def _attendance_site(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/local/ubion/user/":
        return httpx.Response(200, text=COURSES_HTML)
    if path == "/course/view.php" and request.url.params.get("id") == "100":
        return httpx.Response(200, text=COURSE_100_HTML)
    if path == "/course/view.php" and request.url.params.get("id") == "200":
        return httpx.Response(200, text=COURSE_200_HTML)
    if path == "/mod/attendance/view.php":
        return httpx.Response(200, text=VALID_HTML)
    return httpx.Response(404, text="not found")


@pytest.mark.asyncio
async def test_get_attendance_cell_summary_absences():
    """Absence extraction through the real client path, cell-summary layout."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ubion/user/":
            return httpx.Response(200, text=COURSES_HTML.split('<tr><td>2</td>')[0] + "</table>")
        if request.url.path == "/course/view.php":
            return httpx.Response(200, text=COURSE_100_HTML)
        if request.url.path == "/mod/attendance/view.php":
            return httpx.Response(200, text=CELL_SUMMARY_HTML)
        return httpx.Response(404)

    client = _mock_client(handler)
    items = await client.get_attendance()
    assert items[0].available is True
    assert items[0].absences == 5
    assert items[0].late == 0


@pytest.mark.asyncio
async def test_get_attendance_late_zero_absent_positive():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ubion/user/":
            return httpx.Response(200, text=COURSES_HTML.split('<tr><td>2</td>')[0] + "</table>")
        if request.url.path == "/course/view.php":
            return httpx.Response(200, text=COURSE_100_HTML)
        if request.url.path == "/mod/attendance/view.php":
            return httpx.Response(200, text=LATE_ZERO_ABSENT_HTML)
        return httpx.Response(404)

    client = _mock_client(handler)
    items = await client.get_attendance()
    assert items[0].absences == 5
    assert items[0].late == 0  # Late not confused with Absent


@pytest.mark.asyncio
async def test_get_attendance_unreported_absence_stays_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ubion/user/":
            return httpx.Response(200, text=COURSES_HTML.split('<tr><td>2</td>')[0] + "</table>")
        if request.url.path == "/course/view.php":
            return httpx.Response(200, text=COURSE_100_HTML)
        if request.url.path == "/mod/attendance/view.php":
            return httpx.Response(200, text=NO_ABSENCE_REPORTED_HTML)
        return httpx.Response(404)

    client = _mock_client(handler)
    items = await client.get_attendance()
    assert items[0].available is True   # attendance IS tracked
    assert items[0].absences is None    # but no absence figure → not 0


@pytest.mark.asyncio
async def test_get_attendance_multiple_courses():
    client = _mock_client(_attendance_site)
    items = await client.get_attendance()
    assert len(items) == 2
    by_id = {c.course_external_id: c for c in items}
    assert by_id["100"].available is True
    assert by_id["100"].absences == 5
    assert by_id["100"].present == 18
    assert by_id["100"].late == 1
    assert len(by_id["100"].records) == 3
    assert by_id["200"].available is False
    assert by_id["200"].absences is None  # must NOT be 0


@pytest.mark.asyncio
async def test_get_attendance_course_with_zero_absences():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ubion/user/":
            return httpx.Response(200, text=COURSES_HTML.split('<tr><td>2</td>')[0] + "</table>")
        if request.url.path == "/course/view.php":
            return httpx.Response(200, text=COURSE_100_HTML)
        if request.url.path == "/mod/attendance/view.php":
            return httpx.Response(200, text=ZERO_ABSENCE_HTML)
        return httpx.Response(404)

    client = _mock_client(handler)
    items = await client.get_attendance()
    assert items[0].available is True
    assert items[0].absences == 0  # real zero, distinct from unavailable


@pytest.mark.asyncio
async def test_get_attendance_blank_section_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ubion/user/":
            return httpx.Response(200, text=COURSES_HTML.split('<tr><td>2</td>')[0] + "</table>")
        if request.url.path == "/course/view.php":
            return httpx.Response(200, text=COURSE_100_HTML)
        if request.url.path == "/mod/attendance/view.php":
            return httpx.Response(200, text=BLANK_HTML)
        return httpx.Response(404)

    client = _mock_client(handler)
    items = await client.get_attendance()
    assert items[0].available is False
    assert items[0].absences is None  # blank must never become 0


@pytest.mark.asyncio
async def test_network_failure_is_not_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _mock_client(handler)
    with pytest.raises(httpx.ConnectError):
        await client.get_attendance()


# --------------------------- formatting ---------------------------


def test_format_unavailable():
    ca = CourseAttendance(course_external_id="1", course_name="Calculus", available=False)
    text = attendance_service.format_course_attendance(ca)
    assert "not available" in text
    assert "professor" in text.lower()


def test_format_detail_shows_only_available_fields():
    ca = CourseAttendance(
        course_external_id="1", course_name="Calculus", available=True,
        absences=5, late=1, present=18,
        records=[AttendanceRecord("Oct 7", "Present"), AttendanceRecord("Oct 5", "Absent")],
    )
    text = attendance_service.format_course_attendance(ca)
    assert "Absences: 5" in text and "Late: 1" in text and "Present: 18" in text
    assert "Excused" not in text
    assert "Oct 7 — Present" in text


def test_format_zero_absences_is_shown_as_zero():
    ca = CourseAttendance("1", "Calculus", True, absences=0, late=0)
    text = attendance_service.format_course_attendance(ca)
    assert "Absences: 0" in text
    assert "Late: 0" in text
    assert "not reported" not in text


def test_format_unreported_absence_is_explicit():
    """Tracked course with no absence figure → explicit message, never 0."""
    ca = CourseAttendance("1", "Calculus", True, absences=None, late=0)
    text = attendance_service.format_course_attendance(ca)
    assert "Absences: 0" not in text
    assert "not reported by E-Class" in text


def test_format_row_reports_absences_before_late():
    from app.database.models.attendance import Attendance as _Att
    from datetime import datetime, timezone

    row = _Att(
        user_id=1, course_external_id="1", course_name="Engineering Communications",
        available=True, absences=5, late=0, present=18, excused=0,
        updated_at=datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc),
    )
    text = attendance_service.format_attendance_row(row, "Asia/Tashkent")
    assert "Absences: 5" in text
    assert "Late: 0" in text
    assert text.index("Absences: 5") < text.index("Late: 0")


# --------------------------- handler fixtures ---------------------------


class _Ctx:
    def __init__(self, s):
        self.s = s

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, *a):
        return False


def _patch_session_factory(monkeypatch, db, *modules):
    for mod in modules:
        monkeypatch.setattr(mod, "get_session_factory", lambda: (lambda: _Ctx(db)))


class FakeMessage:
    def __init__(self, text="/attendance", tg_id=12345, username="test"):
        self.text = text
        self.from_user = SimpleNamespace(id=tg_id, username=username, first_name="T")
        self.answers = []
        self.edits = []

    async def answer(self, text, **kw):
        self.answers.append(SimpleNamespace(text=text, **kw))
        return self

    async def edit_text(self, text, **kw):
        self.edits.append(SimpleNamespace(text=text, **kw))
        return self


class FakeCallback:
    def __init__(self, data, tg_id=12345, username="test"):
        self.data = data
        self.from_user = SimpleNamespace(id=tg_id, username=username)
        self.message = FakeMessage(tg_id=tg_id, username=username)
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


async def _user_with_account(db, active=True) -> User:
    u = User(telegram_id=12345, username="test", timezone="Asia/Tashkent")
    db.add(u)
    await db.flush()
    db.add(EClassAccount(user_id=u.id, username="u123", session_data="x", is_active=active))
    db.add(Course(user_id=u.id, external_id="100", name="Calculus"))
    db.add(Course(user_id=u.id, external_id="200", name="Programming"))
    await db.commit()
    return u


async def _seed_attendance(db, u: User, rows) -> list:
    from datetime import datetime, timezone

    from app.database.models.attendance import Attendance

    out = []
    for course_external_id, name, available, absences, present, late, excused, recs in rows:
        import json as _json

        row = Attendance(
            user_id=u.id, course_external_id=course_external_id, course_name=name,
            available=available, absences=absences, present=present, late=late,
            excused=excused,
            records_json=_json.dumps(recs) if recs else None,
            updated_at=datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc),
        )
        db.add(row)
        out.append(row)
    await db.commit()
    return out


async def _user_with_account(db, active=True, last_sync=None) -> User:
    from datetime import datetime, timezone

    u = User(telegram_id=12345, username="test", timezone="Asia/Tashkent")
    db.add(u)
    await db.flush()
    db.add(EClassAccount(
        user_id=u.id, username="u123", session_data="x", is_active=active,
        last_sync=last_sync or datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc),
    ))
    db.add(Course(user_id=u.id, external_id="100", name="Calculus"))
    db.add(Course(user_id=u.id, external_id="200", name="Programming"))
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_attendance_command_shows_all_courses(db, monkeypatch):
    u = await _user_with_account(db)
    await _seed_attendance(db, u, [
        ("100", "Calculus", True, 5, 18, 1, 0, None),
        ("200", "Programming", False, None, None, None, None, None),
    ])
    _patch_session_factory(monkeypatch, db, att_mod)
    msg = FakeMessage()
    await att_mod.cmd_attendance(msg)
    assert len(msg.answers) == 1
    kb = msg.answers[0].reply_markup
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert labels == ["Calculus", "Programming"]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert callbacks == ["att:course:100", "att:course:200"]


@pytest.mark.asyncio
async def test_attendance_works_without_valid_eclass_session(db, monkeypatch):
    """/attendance must not require a live E-Class session: inactive account still shows stored data."""
    u = await _user_with_account(db, active=False)
    await _seed_attendance(db, u, [("100", "Calculus", True, 5, 18, 1, 0, None)])
    _patch_session_factory(monkeypatch, db, att_mod)
    msg = FakeMessage()
    await att_mod.cmd_attendance(msg)
    assert "📊 Attendance" in msg.answers[0].text
    # stale warning because the account is inactive
    assert "⚠️" in msg.answers[0].text


@pytest.mark.asyncio
async def test_course_detail_shows_last_updated(db, monkeypatch):
    u = await _user_with_account(db)
    await _seed_attendance(db, u, [
        ("100", "Calculus", True, 5, 18, 1, 0, [{"date": "Oct 7", "status": "Present"}]),
    ])
    _patch_session_factory(monkeypatch, db, att_mod)
    call = FakeCallback("att:course:100")
    await att_mod.att_course(call)
    text = call.message.edits[0].text
    assert "Calculus" in text and "Absences: 5" in text and "Late: 1" in text
    assert "Present: 18" in text and "Excused: 0" in text
    assert "Last updated: 8 Oct 2026, 16:30" in text  # 11:30 UTC = 16:30 Asia/Tashkent
    buttons = [b for row in call.message.edits[0].reply_markup.inline_keyboard for b in row]
    assert buttons[0].callback_data == "att:list"


@pytest.mark.asyncio
async def test_course_with_unavailable_attendance(db, monkeypatch):
    u = await _user_with_account(db)
    await _seed_attendance(db, u, [("200", "Programming", False, None, None, None, None, None)])
    _patch_session_factory(monkeypatch, db, att_mod)
    call = FakeCallback("att:course:200")
    await att_mod.att_course(call)
    text = call.message.edits[0].text
    assert "not available" in text and "professor" in text.lower()
    assert "Absences: 0" not in text
    assert "Last updated:" in text


@pytest.mark.asyncio
async def test_attendance_displays_persisted_absence_after_sync(db, monkeypatch):
    """A sync that fills in absences must be visible in /attendance."""
    from datetime import datetime, timezone

    u = await _user_with_account(db)
    account = (await db.execute(__import__("sqlalchemy").select(EClassAccount))).scalar_one()

    # Row exists from an earlier sync that reported no absence figure.
    await _seed_attendance(db, u, [("100", "Calculus", True, None, None, 0, None, None)])
    _patch_session_factory(monkeypatch, db, att_mod)

    call = FakeCallback("att:course:100")
    await att_mod.att_course(call)
    assert "not reported by E-Class" in call.message.edits[0].text
    assert "Absences: 0" not in call.message.edits[0].text

    # New sync reports absences=5 → same row updated, value now displayed.
    client = FakeEClassClient(items=[
        CourseAttendance("100", "Calculus", True, absences=5, present=18, late=0),
    ])
    await attendance_service.sync_attendance(
        db, account, client, datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)
    )
    await db.commit()

    rows = await attendance_service.get_persisted(db, u.id)
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0].absences == 5

    call2 = FakeCallback("att:course:100")
    await att_mod.att_course(call2)
    text = call2.message.edits[0].text
    assert "Absences: 5" in text
    assert "not reported by E-Class" not in text


@pytest.mark.asyncio
async def test_back_button_returns_to_course_list(db, monkeypatch):
    u = await _user_with_account(db)
    await _seed_attendance(db, u, [
        ("100", "Calculus", True, 5, 18, 1, 0, None),
        ("200", "Programming", False, None, None, None, None, None),
    ])
    _patch_session_factory(monkeypatch, db, att_mod)
    call = FakeCallback("att:list")
    await att_mod.att_back_to_courses(call)
    kb = call.message.edits[0].reply_markup
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == ["Calculus", "Programming"]


@pytest.mark.asyncio
async def test_stale_warning_shown_when_sync_failed(db, monkeypatch):
    from datetime import datetime, timezone

    newer = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)  # last E-Class attempt
    u = await _user_with_account(db, last_sync=newer)  # no attendance update since
    await _seed_attendance(db, u, [("100", "Calculus", True, 5, 18, 1, 0, None)])
    _patch_session_factory(monkeypatch, db, att_mod)
    call = FakeCallback("att:course:100")
    await att_mod.att_course(call)
    text = call.message.edits[0].text
    assert "⚠️ Unable to update attendance" in text
    assert "Last updated: 8 Oct 2026, 16:30" in text


# --------------------------- sync persistence ---------------------------


class FakeEClassClient:
    def __init__(self, items=None, exc=None):
        self._items = items or []
        self._exc = exc

    async def get_courses(self):
        return []

    async def get_assignments(self):
        return []

    async def get_quizzes(self):
        return []

    async def get_attendance(self):
        if self._exc:
            raise self._exc
        return self._items

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_sync_attendance_saves_data(db):
    u = await _user_with_account(db)
    account = (await db.execute(__import__("sqlalchemy").select(EClassAccount))).scalar_one()
    from datetime import datetime, timezone

    client = FakeEClassClient(items=[
        CourseAttendance("100", "Calculus", True, absences=5, present=18, late=1, excused=0,
                         records=[AttendanceRecord("Oct 7", "Present")]),
        CourseAttendance("200", "Programming", False),
    ])
    await attendance_service.sync_attendance(db, account, client, datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc))
    await db.commit()
    rows = await attendance_service.get_persisted(db, u.id)
    assert len(rows) == 2
    by_id = {r.course_external_id: r for r in rows}
    assert by_id["100"].available and by_id["100"].absences == 5 and by_id["100"].late == 1
    assert by_id["100"].records() == [{"date": "Oct 7", "status": "Present"}]
    assert by_id["200"].available is False and by_id["200"].absences is None


@pytest.mark.asyncio
async def test_repeated_sync_updates_not_duplicates(db):
    u = await _user_with_account(db)
    account = (await db.execute(__import__("sqlalchemy").select(EClassAccount))).scalar_one()
    from datetime import datetime, timezone

    now = datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc)
    client = FakeEClassClient(items=[CourseAttendance("100", "Calculus", True, absences=5, present=18)])
    await attendance_service.sync_attendance(db, account, client, now)
    await db.commit()
    client2 = FakeEClassClient(items=[CourseAttendance("100", "Calculus", True, absences=6, present=19)])
    await attendance_service.sync_attendance(db, account, client2, now)
    await db.commit()
    rows = await attendance_service.get_persisted(db, u.id)
    assert len(rows) == 1
    assert rows[0].absences == 6 and rows[0].present == 19


@pytest.mark.asyncio
async def test_network_failure_keeps_previous_attendance(db):
    u = await _user_with_account(db)
    account = (await db.execute(__import__("sqlalchemy").select(EClassAccount))).scalar_one()
    from datetime import datetime, timezone

    now = datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc)
    client = FakeEClassClient(items=[CourseAttendance("100", "Calculus", True, absences=5, present=18)])
    await attendance_service.sync_attendance(db, account, client, now)
    await db.commit()
    with pytest.raises(httpx.ConnectError):
        await attendance_service.sync_attendance(
            db, account, FakeEClassClient(exc=httpx.ConnectError("boom")), now,
        )
    rows = await attendance_service.get_persisted(db, u.id)
    assert rows[0].absences == 5  # unchanged — failure is never converted to 0


@pytest.mark.asyncio
async def test_sync_user_refreshes_attendance(db, monkeypatch):
    from datetime import datetime, timezone

    from app.database.models.group import Group
    from app.services import sync_service

    g = Group(name="ICE-24-1")
    db.add(g)
    await db.flush()
    u = User(telegram_id=12345, username="test", timezone="Asia/Tashkent", group_id=g.id)
    db.add(u)
    await db.flush()
    db.add(EClassAccount(user_id=u.id, username="u123", session_data="x", is_active=True))
    await db.commit()

    items = [CourseAttendance("100", "Calculus", True, absences=3, present=20)]
    fake = FakeEClassClient(items=items)

    class FakeEdu:
        async def get_timetable(self, *a, **kw):
            return []

    monkeypatch.setattr(sync_service, "EduPageWebClient", lambda: FakeEdu())

    async def _cfa(session, account):
        return fake

    monkeypatch.setattr(sync_service, "client_for_account", _cfa)
    account = (await db.execute(__import__("sqlalchemy").select(EClassAccount))).scalar_one()
    outcome = await sync_service.sync_user(db, account)
    assert outcome.attendance_ok is True
    rows = await attendance_service.get_persisted(db, u.id)
    assert len(rows) == 1 and rows[0].absences == 3


@pytest.mark.asyncio
async def test_expired_session_does_not_overwrite_attendance(db, monkeypatch):
    from datetime import datetime, timezone

    from app.services import sync_service
    from app.services.eclass_session import SessionExpiredError

    u = await _user_with_account(db)
    account = (await db.execute(__import__("sqlalchemy").select(EClassAccount))).scalar_one()
    now = datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc)
    client = FakeEClassClient(items=[CourseAttendance("100", "Calculus", True, absences=5)])
    await attendance_service.sync_attendance(db, account, client, now)
    await db.commit()

    async def _expired(session, acc):
        raise SessionExpiredError("expired")

    monkeypatch.setattr(sync_service, "client_for_account", _expired)
    outcome = await sync_service.sync_user(db, account)
    assert outcome.session_expired is True
    rows = await attendance_service.get_persisted(db, u.id)
    assert rows[0].absences == 5  # preserved


# --------------------------- weekly report ---------------------------


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_weekly_report_generated_for_active_users(db, monkeypatch):
    u = await _user_with_account(db)
    await _seed_attendance(db, u, [
        ("100", "Calculus", True, 5, 18, 1, 0, None),
        ("200", "Programming", False, None, None, None, None, None),
        ("300", "Physics", True, 0, 20, 0, 0, None),
    ])
    _patch_session_factory(monkeypatch, db, jobs)
    bot = FakeBot()
    await jobs.send_weekly_attendance_reports(bot)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "Weekly Attendance Report" in text
    assert "Calculus — 5 absences" in text
    assert "Programming — Attendance not available in E-Class" in text
    assert "Physics — 0 absences" in text


@pytest.mark.asyncio
async def test_weekly_report_reports_missing_absence_as_unavailable(db, monkeypatch):
    """Tracked course without an absence figure must not read as 0 absences."""
    u = await _user_with_account(db)
    await _seed_attendance(db, u, [
        ("100", "Calculus", True, None, None, 0, None, None),
    ])
    _patch_session_factory(monkeypatch, db, jobs)
    bot = FakeBot()
    await jobs.send_weekly_attendance_reports(bot)
    text = bot.sent[0][1]
    assert "Calculus — 0 absences" not in text
    assert "absence count not reported by E-Class" in text


@pytest.mark.asyncio
async def test_weekly_report_deduplicated(db, monkeypatch):
    u = await _user_with_account(db)
    await _seed_attendance(db, u, [("100", "Calculus", True, 5, 18, 1, 0, None)])
    _patch_session_factory(monkeypatch, db, jobs)
    bot = FakeBot()
    await jobs.send_weekly_attendance_reports(bot)
    await jobs.send_weekly_attendance_reports(bot)
    assert len(bot.sent) == 1  # same week → second run skipped


@pytest.mark.asyncio
async def test_weekly_report_skips_inactive_and_no_account(db, monkeypatch):
    await _user_with_account(db, active=False)
    _patch_session_factory(monkeypatch, db, jobs)
    bot = FakeBot()
    await jobs.send_weekly_attendance_reports(bot)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_weekly_report_marks_stale_data(db, monkeypatch):
    from datetime import datetime, timezone

    newer = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    u = await _user_with_account(db, last_sync=newer)
    await _seed_attendance(db, u, [("100", "Calculus", True, 5, 18, 1, 0, None)])
    _patch_session_factory(monkeypatch, db, jobs)
    bot = FakeBot()
    await jobs.send_weekly_attendance_reports(bot)
    assert len(bot.sent) == 1
    assert "⚠️" in bot.sent[0][1] and "Last successful sync" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_dedup_key_stable_per_week(db, monkeypatch):
    u = await _user_with_account(db)
    assert await notification_service.due_weekly_attendance(db, u.id, "2026-10-05") is True
    await notification_service.mark_weekly_attendance_sent(db, u.id, "2026-10-05")
    assert await notification_service.due_weekly_attendance(db, u.id, "2026-10-05") is False
    # a different week fires again
    assert await notification_service.due_weekly_attendance(db, u.id, "2026-10-12") is True
