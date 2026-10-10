"""Regression tests for the missing grade-notification incident.

Root cause: quiz grades published only on the quiz attempt page were never
read (the index prints "-"), the "X out of Y" grade format was mishandled,
and no score-change detection existed, so a corrected grade could never
notify. Telegram is mocked throughout; no real message is ever sent.
"""

import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database.models.assignment import Assignment
from app.database.models.eclass_account import EClassAccount
from app.database.models.group import Group
from app.database.models.notification import NotificationRecord
from app.database.models.user import User
from app.eclass.models import Assignment as EAssignment
from app.eclass.web_client import EClassWebClient, _parse_attempt_mark, _parse_grade
from app.services.academic_sync import (
    score_notification_sent,
    score_notification_sent_for_item,
    score_ref,
    sync_academic_records,
)
from app.services.notification_service import send_score_notifications

FUTURE = datetime.now(timezone.utc) + timedelta(days=7)
PAST = datetime.now(timezone.utc) - timedelta(days=7)


# --------------------------- helpers ---------------------------


def eitem(title, kind="homework", deadline=FUTURE, status=None, score=None,
          max_score=None, eid=None):
    return EAssignment(
        external_id=eid or title.lower().replace(" ", "_"),
        course_external_id="C1", course_name="Database", title=title,
        deadline=deadline, submission_status=status, kind=kind,
        score=score, max_score=max_score,
    )


async def _user(db):
    g = Group(name="ICE-24-1")
    db.add(g)
    await db.flush()
    u = User(telegram_id=99, username="t", timezone="Asia/Tashkent", group_id=g.id)
    db.add(u)
    await db.commit()
    return u


class FakeBot:
    """Records messages; can be told to fail like Telegram would."""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send_message(self, chat_id, text):
        if not self.ok:
            raise httpx.ConnectError("telegram unreachable")
        self.sent.append((chat_id, text))


async def _sent_refs(db, uid):
    rows = (await db.execute(select(NotificationRecord).where(
        NotificationRecord.user_id == uid, NotificationRecord.kind == "score"))).scalars().all()
    return {r.ref_id for r in rows}


# --------------------------- grade cell parsing ---------------------------


@pytest.mark.parametrize("raw,expected", [
    ("8.00/10.00", ("8.00", "10.00")),
    ("8.00 out of 10.00", ("8.00", "10.00")),
    ("8.00 Out Of 10.00", ("8.00", "10.00")),
    ("8.00", ("8.00", None)),          # max not published yet
    ("0.00/10.00", ("0.00", "10.00")),  # explicit zero is a real grade
    ("0.00", ("0.00", None)),
    ("80%", ("80", None)),
    ("-", (None, None)),
    ("", (None, None)),
    ("None", (None, None)),
    ("8.00 (60%)", (None, None)),       # ambiguous: refuse to guess
])
def test_parse_grade_formats(raw, expected):
    assert _parse_grade(raw) == expected


def test_parse_attempt_mark_from_moodle_summary():
    detail = """
    <h4>Summary of your previous attempts</h4>
    <table><tr><th>Attempt</th><th>State</th><th>Marks</th><th>Raw</th>
    <th>Marked out of</th><th>Time spent</th><th>Grade</th></tr>
    <tr><td>1</td><td>Finished</td><td>8.00</td><td>8.00</td><td>10.00</td>
    <td>00:01:00</td><td>Passed</td></tr></table>
    """
    assert _parse_attempt_mark(detail) == ("8.00", "10.00")


def test_parse_attempt_mark_from_marks_line():
    assert _parse_attempt_mark("<p>Marks: 7.50/10.00</p>") == ("7.50", "10.00")


def test_parse_attempt_mark_absent():
    assert _parse_attempt_mark("<p>This quiz is not available yet.</p>") == (None, None)


# --------------------------- client: quiz mark on attempt page ---------------------------

COURSES_HTML = (
    '<tr><td>1</td><td><div><span class="label label-course">OFFLINE</span> '
    '<a href="https://eclass.example/course/view.php?id=2603" '
    'class="coursefullname">Operating System</a></div></td>'
    '<td class="text-center">Prof</td><td>29</td><td>Student</td></tr>'
)
QUIZ_INDEX_DASH = """
<table class="generaltable">
<tr><th>#</th><th>Activity name</th><th>Deadline</th><th>Grade</th><th>Review</th></tr>
<tr><td>0</td><td><a href="view.php?id=71611">Quiz 3</a></td>
<td class="cell c2">2026-10-09 18:59</td>
<td class="cell c3">-</td>
<td class="cell c4">Review attempts</td></tr>
</table>
"""
QUIZ_ATTEMPT = """
<div class="mainbox"><h4>Summary of your previous attempts</h4>
<table><tr><th>Attempt</th><th>State</th><th>Marks</th><th>Raw</th>
<th>Marked out of</th><th>Grade</th></tr>
<tr><td>1</td><td>Finished</td><td>8.00</td><td>8.00</td><td>10.00</td>
<td>Passed</td></tr></table>
<p>Marks: 8.00/10.00</p></div>
"""


def _quiz_client(index_html, detail_html):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/local/ubion/user/":
            return httpx.Response(200, text=COURSES_HTML)
        if p == "/mod/quiz/index.php":
            return httpx.Response(200, text=index_html)
        if p == "/mod/quiz/view.php":
            return httpx.Response(200, text=detail_html)
        return httpx.Response(404, text="nf")

    c = EClassWebClient(base_url="https://eclass.example")
    c._client = httpx.AsyncClient(
        base_url="https://eclass.example", follow_redirects=True,
        transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_quiz_mark_recovered_from_attempt_page():
    """THE INCIDENT: index prints '-', the real mark lives on the attempt page."""
    client = _quiz_client(QUIZ_INDEX_DASH, QUIZ_ATTEMPT)
    quizzes = await client.get_quizzes()
    await client.aclose()
    assert len(quizzes) == 1
    q = quizzes[0]
    assert q.score == "8.00"
    assert q.max_score == "10.00"
    assert q.submission_status == "Completed"


@pytest.mark.asyncio
async def test_quiz_zero_mark_from_attempt_page():
    attempt = QUIZ_ATTEMPT.replace("8.00", "0.00")
    client = _quiz_client(QUIZ_INDEX_DASH, attempt)
    quizzes = await client.get_quizzes()
    await client.aclose()
    assert quizzes[0].score == "0.00"   # explicit zero, not None


@pytest.mark.asyncio
async def test_quiz_without_attempt_page_stays_ungraded():
    """No attempt summary -> no fabricated grade."""
    client = _quiz_client(QUIZ_INDEX_DASH, "<p>No attempts yet.</p>")
    quizzes = await client.get_quizzes()
    await client.aclose()
    assert quizzes[0].score is None
    assert quizzes[0].max_score is None


@pytest.mark.asyncio
async def test_quiz_index_grade_wins_over_attempt_page():
    index = QUIZ_INDEX_DASH.replace('<td class="cell c3">-</td>',
                                    '<td class="cell c3">9.00/10.00</td>')
    client = _quiz_client(index, QUIZ_ATTEMPT)
    quizzes = await client.get_quizzes()
    await client.aclose()
    assert quizzes[0].score == "9.00"


ASSIGN_INDEX = """
<table class="generaltable">
<tr><th>#</th><th>Assignment</th><th>Deadline</th><th>Submitted</th><th>Grade</th></tr>
<tr><td>1</td><td><a href="https://eclass.example/mod/assign/view.php?id=71966">HW 2</a></td>
<td class="cell c2">2026-10-24 18:55</td>
<td class="cell c3">Submitted for grading</td>
<td class="cell c4 lastcol">{grade}</td></tr>
</table>
"""


def _assign_client(grade):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/local/ubion/user/":
            return httpx.Response(200, text=COURSES_HTML)
        if p == "/mod/assign/index.php":
            return httpx.Response(200, text=ASSIGN_INDEX.format(grade=grade))
        return httpx.Response(404, text="nf")

    c = EClassWebClient(base_url="https://eclass.example")
    c._client = httpx.AsyncClient(
        base_url="https://eclass.example", follow_redirects=True,
        transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_assignment_out_of_format_parsed():
    client = _assign_client("8.00 out of 10.00")
    items = await client.get_assignments()
    await client.aclose()
    assert items[0].score == "8.00" and items[0].max_score == "10.00"


# --------------------------- notification pipeline ---------------------------


@pytest.mark.asyncio
async def test_existing_ungraded_assignment_becomes_graded_and_notifies(db):
    u = await _user(db)
    await sync_academic_records(db, u.id, [
        eitem("Hw", deadline=PAST, status="Submitted for grading")])
    graded = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                    score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, graded)
    assert len(new) == 1 and new[0].score == "87"

    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1 and "87/100" in bot.sent[0][1]
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert rows == []   # cleaned up only after delivery


@pytest.mark.asyncio
async def test_first_discovery_with_grade_notifies(db):
    """An already-graded item seen for the first time must not be skipped."""
    u = await _user(db)
    new = await sync_academic_records(db, u.id, [
        eitem("Old graded", deadline=PAST, status="Submitted for grading",
              score="12", max_score="20")])
    assert len(new) == 1
    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1 and "12/20" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_explicit_zero_score_notifies(db):
    """0 is a real grade and must notify; it is not 'no grade'."""
    u = await _user(db)
    new = await sync_academic_records(db, u.id, [
        eitem("Zero", deadline=PAST, status="Submitted for grading",
              score="0", max_score="10")])
    assert len(new) == 1 and new[0].score == "0"
    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1 and "0/10" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_quiz_grade_notifies(db):
    u = await _user(db)
    new = await sync_academic_records(db, u.id, [
        eitem("Quiz 3", kind="quiz", deadline=PAST, status="Completed",
              score="8", max_score="10")])
    assert len(new) == 1
    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1 and "🧪" in bot.sent[0][1] and "8/10" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_missing_score_generates_no_notification(db):
    """A missing score must never be turned into a fake grade."""
    u = await _user(db)
    new = await sync_academic_records(db, u.id, [
        eitem("Awaiting", deadline=PAST, status="Submitted for grading")])
    assert new == []
    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert bot.sent == []
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert len(rows) == 1 and rows[0].score is None   # kept, still ungraded


@pytest.mark.asyncio
async def test_unchanged_score_no_duplicate_on_next_sync(db):
    u = await _user(db)
    items = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                   score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, items)
    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1

    for _ in range(3):
        again = await sync_academic_records(db, u.id, items)
        assert again == []                      # nothing re-queued
        await send_score_notifications(db, bot, u, again)
    assert len(bot.sent) == 1                   # no duplicate message


@pytest.mark.asyncio
async def test_changed_score_notifies_again(db):
    """Policy: a corrected grade is a new event and gets its own message."""
    u = await _user(db)
    first = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                   score="5", max_score="10")]
    new = await sync_academic_records(db, u.id, first)
    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1 and "5/10" in bot.sent[0][1]

    corrected = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                       score="7", max_score="10")]
    new2 = await sync_academic_records(db, u.id, corrected)
    assert len(new2) == 1 and new2[0].score == "7"
    await send_score_notifications(db, bot, u, new2)
    assert len(bot.sent) == 2 and "7/10" in bot.sent[1][1]

    # and the corrected grade is not re-sent either
    assert await sync_academic_records(db, u.id, corrected) == []


@pytest.mark.asyncio
async def test_telegram_failure_stays_retryable(db):
    """A failed send must not consume the dedup key."""
    u = await _user(db)
    items = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                   score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, items)

    failing = FakeBot(ok=False)
    await send_score_notifications(db, failing, u, new)
    assert failing.sent == []
    assert await _sent_refs(db, u.id) == set()          # nothing recorded

    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert len(rows) == 1 and rows[0].score == "87"   # kept, not cleaned up

    retry = await sync_academic_records(db, u.id, items)
    assert len(retry) == 1                        # still eligible
    ok = FakeBot()
    await send_score_notifications(db, ok, u, retry)
    assert len(ok.sent) == 1
    assert await _sent_refs(db, u.id) == {score_ref("hw", "87")}


@pytest.mark.asyncio
async def test_process_interruption_before_commit_keeps_pending(db, monkeypatch):
    """If the DB commit fails after Telegram accepted, the item stays
    eligible. At-least-once: the message may repeat, the grade is not lost."""
    u = await _user(db)
    items = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                   score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, items)

    uid = u.id   # capture before rollback expires the ORM instance
    bot = FakeBot()

    async def failing_commit():
        # Simulates the process dying after Telegram accepted the message but
        # before the DB write landed.
        raise RuntimeError("process died before commit")

    monkeypatch.setattr(db, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        # a commit failure propagates rather than being silently swallowed
        await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1              # telegram did receive it
    monkeypatch.undo()
    await db.rollback()
    assert await _sent_refs(db, uid) == set()   # dedup key NOT consumed

    # next sync still sees it as pending rather than losing the grade
    retry = await sync_academic_records(db, uid, items)
    assert len(retry) == 1
    assert retry[0].score == "87"


@pytest.mark.asyncio
async def test_successful_delivery_not_repeated_after_reinsert(db):
    """After delivery the row is deleted; a later re-discovery of the same
    grade must not re-notify."""
    u = await _user(db)
    items = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                   score="87", max_score="100")]
    bot = FakeBot()
    await send_score_notifications(db, bot, u, await sync_academic_records(db, u.id, items))
    assert len(bot.sent) == 1
    await sync_academic_records(db, u.id, items)
    await send_score_notifications(db, bot, u, await sync_academic_records(db, u.id, items))
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_failed_delivery_does_not_trigger_cleanup(db):
    u = await _user(db)
    items = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                   score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, items)
    await send_score_notifications(db, FakeBot(ok=False), u, new)
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert len(rows) == 1          # NOT cleaned up: notification never went out


@pytest.mark.asyncio
async def test_legacy_bare_ref_id_is_interpreted(db):
    """Records written before score_ref() used the bare external_id."""
    u = await _user(db)
    db.add(NotificationRecord(user_id=u.id, kind="score", ref_id="hw",
                              sent_at=datetime.now(timezone.utc)))
    await db.commit()
    assert await score_notification_sent_for_item(db, u.id, "hw") is True
    # the same legacy record also suppresses the current grade from re-notifying
    new = await sync_academic_records(db, u.id, [
        eitem("Hw", deadline=PAST, status="Submitted for grading",
              score="87", max_score="100")])
    assert new == []
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_grade_survives_unrelated_sync_failures(db, monkeypatch):
    """A later failed sync must not wipe a stored grade or a pending item."""
    from app.services import sync_service

    u = await _user(db)
    g = db.add(Group(name="OTHER-24-1"))
    await db.flush()
    acc = EClassAccount(user_id=u.id, username="u", session_data="x", is_active=True)
    db.add(acc)
    await db.commit()

    graded = [eitem("Hw", deadline=PAST, status="Submitted for grading",
                    score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, graded)

    class DeadClient:
        async def get_courses(self):
            raise httpx.ConnectError("boom")

        async def get_assignments(self):
            return []

        async def get_quizzes(self):
            return []

        async def get_attendance(self):
            return []

        async def aclose(self):
            pass

    async def _cfa(session, account):
        return DeadClient()

    monkeypatch.setattr(sync_service, "client_for_account", _cfa)
    outcome = await sync_service.sync_user(db, acc)
    assert outcome.session_expired is False

    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert len(rows) == 1 and rows[0].score == "87"   # grade intact, still pending

    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1                          # still deliverable


@pytest.mark.asyncio
async def test_completed_but_ungraded_kept_and_no_deadline_reminder(db):
    """Existing lifecycle behaviour must be unchanged."""
    u = await _user(db)
    new = await sync_academic_records(db, u.id, [
        eitem("Submitted no grade", deadline=PAST, status="Submitted for grading"),
        eitem("Completed quiz no grade", kind="quiz", deadline=PAST, status="Completed"),
    ])
    assert new == []
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert {r.title for r in rows} == {"Submitted no grade", "Completed quiz no grade"}
    assert all(r.score is None for r in rows)


@pytest.mark.asyncio
async def test_max_score_arriving_later_is_not_a_grade_change(db):
    """E-Class may publish the mark first and the maximum later; that must
    not re-notify, so max_score is excluded from the dedup key."""
    assert score_ref("hw", "87") == score_ref("hw", "87")
    u = await _user(db)
    bot = FakeBot()
    new = await sync_academic_records(db, u.id, [
        eitem("Hw", deadline=PAST, status="Submitted for grading",
              score="87", max_score=None)])
    await send_score_notifications(db, bot, u, new)
    assert "87/?" in bot.sent[0][1]

    again = await sync_academic_records(db, u.id, [
        eitem("Hw", deadline=PAST, status="Submitted for grading",
              score="87", max_score="100")])
    assert again == []
    await send_score_notifications(db, bot, u, again)
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_duplicate_suppressed_within_a_single_sync(db):
    """Two EAssignment entries for one external_id notify once."""
    u = await _user(db)
    new = await sync_academic_records(db, u.id, [
        eitem("Hw", eid="dup", deadline=PAST, status="Submitted for grading", score="9"),
        eitem("Hw copy", eid="dup", deadline=PAST, status="Submitted for grading", score="9"),
    ])
    assert len(new) == 1
    bot = FakeBot()
    await send_score_notifications(db, bot, u, new)
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_score_ref_keys_are_distinct_per_grade(db):
    """Grade 5 notifies, grade 7 notifies, grade 7 again does not."""
    u = await _user(db)
    expected_sends = [1, 1, 0]
    for grade, expected in zip(("5", "7", "7"), expected_sends):
        new = await sync_academic_records(db, u.id, [
            eitem("Hw", deadline=PAST, status="Submitted for grading", score=grade)])
        bot = FakeBot()
        await send_score_notifications(db, bot, u, new)
        assert len(bot.sent) == expected, f"grade={grade} sent={len(bot.sent)}"
    assert await _sent_refs(db, u.id) == {score_ref("hw", "5"), score_ref("hw", "7")}