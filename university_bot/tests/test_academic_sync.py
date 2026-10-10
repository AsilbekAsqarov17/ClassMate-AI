"""Watchlist lifecycle tests for app/services/academic_sync.py."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.database.models.assignment import Assignment
from app.database.models.group import Group
from app.database.models.notification import NotificationRecord
from app.database.models.user import User
from app.eclass.models import Assignment as EAssignment
from app.services.academic_sync import (
    is_submitted,
    score_notification_sent,
    score_notification_sent_for_item,
    score_ref,
    sync_academic_records,
)
from app.services.notification_service import send_score_notifications

NOW = datetime.now(timezone.utc)
FUTURE = NOW + timedelta(days=7)
PAST = NOW - timedelta(days=7)


def eitem(title, kind="homework", deadline=FUTURE, status=None, score=None, max_score=None, eid=None):
    return EAssignment(
        external_id=eid or title.lower().replace(' ', '_'),
        course_external_id="C1",
        course_name="Database",
        title=title,
        deadline=deadline,
        submission_status=status,
        kind=kind,
        score=score,
        max_score=max_score,
    )


async def _user(db):
    g = Group(name="ICE-24-1")
    db.add(g)
    await db.flush()
    u = User(telegram_id=99, username="t", timezone="Asia/Tashkent", group_id=g.id)
    db.add(u)
    await db.commit()
    return u


async def count_rows(db, uid):
    return (await db.execute(select(func.count()).select_from(Assignment).where(Assignment.user_id == uid))).scalar()


@pytest.mark.asyncio
async def test_future_assignment_and_quiz_are_stored(db):
    u = await _user(db)
    new = await sync_academic_records(db, u.id, [eitem("HW1"), eitem("Quiz1", kind="quiz")])
    assert await count_rows(db, u.id) == 2
    assert new == []


@pytest.mark.asyncio
async def test_past_unsubmitted_removed(db):
    u = await _user(db)
    await sync_academic_records(db, u.id, [eitem("Old HW", deadline=PAST, status="No submission"),
                                          eitem("Old Quiz", kind="quiz", deadline=PAST, status="No submission")])
    assert await count_rows(db, u.id) == 0


@pytest.mark.asyncio
async def test_future_not_past_kept(db):
    u = await _user(db)
    await sync_academic_records(db, u.id, [eitem("Future HW", deadline=FUTURE, status="No submission"),
                                          eitem("Old Ungraded Submitted", deadline=PAST, status="Submitted for grading")])
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert {r.title for r in rows} == {"Future HW", "Old Ungraded Submitted"}


@pytest.mark.asyncio
async def test_submitted_no_score_is_kept(db):
    u = await _user(db)
    await sync_academic_records(db, u.id, [eitem("Sub HW", deadline=PAST, status="Submitted for grading")])
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert len(rows) == 1 and rows[0].score is None


@pytest.mark.asyncio
async def test_new_score_enqueues_exactly_once_until_sent(db):
    u = await _user(db)
    await sync_academic_records(db, u.id, [eitem("Hw", deadline=PAST, status="Submitted for grading")])
    items = [eitem("Hw", deadline=PAST, status="Submitted for grading", score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, items)
    assert len(new) == 1 and new[0].score == "87"
    # re-sync with same score but notification still not sent -> one notification attempt again (retry path) per sync, not duplicated in one run
    new2 = await sync_academic_records(db, u.id, items)
    assert len(new2) == 1
    assert len(new2[0].external_id) > 0


@pytest.mark.asyncio
async def test_item_deleted_only_after_successful_notification(db):
    u = await _user(db)
    await sync_academic_records(db, u.id, [eitem("Hw", deadline=PAST, status="Submitted for grading")])
    items = [eitem("Hw", deadline=PAST, status="Submitted for grading", score="87", max_score="100")]
    new = await sync_academic_records(db, u.id, items)

    class FakeBot:
        def __init__(self, ok):
            self.ok = ok
            self.sent = []

        async def send_message(self, chat_id, text):
            if not self.ok:
                raise RuntimeError("telegram blocked")
            self.sent.append((chat_id, text))

    failing = FakeBot(ok=False)
    await send_score_notifications(db, failing, u, new)
    rows_fail = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert len(rows_fail) == 1  # item kept after failure

    okbot = FakeBot(ok=True)
    await send_score_notifications(db, okbot, u, (await sync_academic_records(db, u.id, items)))
    rows_ok = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    assert rows_ok == []
    assert okbot.sent and "NEW SCORE" in okbot.sent[0][1] and "87/100" in okbot.sent[0][1]

    # the dedup key is the item's external_id plus the delivered score
    sent = await score_notification_sent(db, u.id, score_ref("hw", "87"))
    assert sent is True
    assert await score_notification_sent_for_item(db, u.id, "hw") is True


@pytest.mark.asyncio
async def test_repeated_sync_no_duplicates(db):
    u = await _user(db)
    items = [eitem("A"), eitem("B", kind="quiz")]
    await sync_academic_records(db, u.id, items)
    await sync_academic_records(db, u.id, items)
    await sync_academic_records(db, u.id, items)
    assert await count_rows(db, u.id) == 2


@pytest.mark.asyncio
async def test_assignments_relevant_only(db):
    from app.services.assignment_service import upcoming
    from app.services.quiz_service import upcoming_quizzes

    u = await _user(db)
    await sync_academic_records(db, u.id, [
        eitem("Future Hw", deadline=FUTURE, status="No submission"),
        eitem("Old HW no sub", deadline=PAST, status="No submission"),
        eitem("Old HW sub", deadline=PAST, status="Submitted for grading"),
        eitem("Quiz future", kind="quiz", deadline=FUTURE),
    ])
    rows = (await db.execute(select(Assignment).where(Assignment.user_id == u.id))).scalars().all()
    titles = {r.title for r in rows}
    assert "Old HW no sub" not in titles
    assert titles == {"Future Hw", "Old HW sub", "Quiz future"}
    from app.services.assignment_service import upcoming
    from app.services.quiz_service import upcoming_quizzes
    hw_up = [a for a in await upcoming(db, u.id) if a.kind != "quiz"]
    assert [a.title for a in hw_up] == ["Future Hw"]
    q_up = await upcoming_quizzes(db, u.id)
    assert [a.title for a in q_up] == ["Quiz future"]


@pytest.mark.asyncio
async def test_past_only_degraded_cleanup(db):
    u = await _user(db)
    await sync_academic_records(db, u.id, [eitem("Exp", deadline=PAST, status="No submission")])
    assert await count_rows(db, u.id) == 0
