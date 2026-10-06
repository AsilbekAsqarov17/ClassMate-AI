"""Quiz availability: /quizzes must show only currently-open, not-completed quizzes.

Uses the real E-Class open_time ("This quiz opened at ..." /
"The quiz will not be available until ...").
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database.models.assignment import Assignment
from app.database.models.group import Group
from app.database.models.user import User
from app.eclass.models import Assignment as EAssignment
from app.services.academic_sync import sync_academic_records
from app.services.notification_service import due_deadline_reminders
from app.services.quiz_service import is_available, upcoming_quizzes

NOW = datetime.now(timezone.utc)
FUTURE_OPEN = NOW + timedelta(days=5)
PAST_OPEN = NOW - timedelta(days=2)
DEADLINE = NOW + timedelta(days=10)


def equiz(title, open_time=None, deadline=DEADLINE, status=None, score=None, eid=None):
    return EAssignment(
        external_id=eid or title.lower().replace(" ", "_"),
        course_external_id="C1",
        course_name="History",
        title=title,
        kind="quiz",
        deadline=deadline,
        open_time=open_time,
        submission_status=status,
        score=score,
    )


@pytest.fixture()
async def quser(db):
    g = Group(name="ICE-24-1")
    db.add(g)
    await db.flush()
    u = User(telegram_id=777, username="qt", timezone="Asia/Tashkent", group_id=g.id)
    db.add(u)
    await db.commit()
    return u


# ---------------- parser ----------------


def test_not_yet_available_regex():
    html = '<p>The quiz will not be available until 2026-10-12 10:00</p>'
    m = re.search(r"The quiz will not be available until ([^<]+)", html)
    assert m and m.group(1) == "2026-10-12 10:00"


def test_opened_regex():
    html = '<p>This quiz opened at 2026-10-05 10:00</p>'
    m = re.search(r"This quiz opened at ([^<]+)", html)
    assert m and m.group(1) == "2026-10-05 10:00"


# ---------------- is_available ----------------


def test_is_available_rules():
    q = Assignment(user_id=1, external_id="x", course_name="C", title="T")
    q.open_time = None
    assert is_available(q, NOW) is True
    q.open_time = PAST_OPEN
    assert is_available(q, NOW) is True
    q.open_time = FUTURE_OPEN
    assert is_available(q, NOW) is False


# ---------------- sync populates open_time ----------------


@pytest.mark.asyncio
async def test_sync_stores_open_time(db, quser):
    await sync_academic_records(db, quser.id, [equiz("MCQS-5", open_time=FUTURE_OPEN)])
    row = (await db.execute(select(Assignment).where(Assignment.user_id == quser.id))).scalar_one()
    assert row.open_time is not None
    assert row.open_time.replace(tzinfo=timezone.utc) == FUTURE_OPEN


# ---------------- /quizzes filtering ----------------


@pytest.mark.asyncio
async def test_unopened_quiz_hidden(db, quser):
    await sync_academic_records(db, quser.id, [
        equiz("MCQS-5", open_time=FUTURE_OPEN),           # not available yet -> hidden
        equiz("Quiz 3", open_time=PAST_OPEN),             # open + not completed -> shown
        equiz("MCQS-4", open_time=PAST_OPEN, status="Completed"),  # completed -> hidden
        equiz("Legacy", open_time=None),                  # unknown open time -> shown
    ])
    up = await upcoming_quizzes(db, quser.id)
    assert [q.title for q in up] == ["Quiz 3", "Legacy"]


@pytest.mark.asyncio
async def test_quiz_becomes_visible_after_open_time(db, quser):
    """Once now >= open_time the quiz appears automatically (no data change)."""
    opens_soon = NOW + timedelta(hours=1)
    await sync_academic_records(db, quser.id, [equiz("Soon", open_time=opens_soon)])
    assert await upcoming_quizzes(db, quser.id) == []
    later = NOW + timedelta(hours=2)
    result = await db.execute(
        select(Assignment).where(Assignment.user_id == quser.id, Assignment.kind == "quiz")
    )
    visible = [q for q in result.scalars() if is_available(q, later)]
    assert [q.title for q in visible] == ["Soon"]


# ---------------- reminders ----------------


@pytest.mark.asyncio
async def test_no_reminder_for_unopened_quiz(db, quser):
    deadline = NOW + timedelta(hours=47, minutes=58)  # 48h reminder due right now
    db.add(Assignment(
        user_id=quser.id, external_id="q1", course_name="C", title="MCQS-5",
        kind="quiz", deadline=deadline, open_time=NOW + timedelta(hours=24),
        submission_status=None,
    ))
    await db.commit()
    assert await due_deadline_reminders(db, quser, NOW) == []


@pytest.mark.asyncio
async def test_reminder_fires_for_opened_quiz(db, quser):
    deadline = NOW + timedelta(hours=47, minutes=58)
    db.add(Assignment(
        user_id=quser.id, external_id="q2", course_name="C", title="Quiz 3",
        kind="quiz", deadline=deadline, open_time=NOW - timedelta(hours=1),
        submission_status=None,
    ))
    await db.commit()
    reminders = await due_deadline_reminders(db, quser, NOW)
    assert len(reminders) == 1
    assert reminders[0][1] == "deadline_48h"
    # and never again
    assert await due_deadline_reminders(db, quser, NOW) == []
