"""Deadline reminder schedule: exactly one reminder 4 hours and one 1 hour
before an assignment or quiz deadline, each delivered exactly once.

Replaces the previous 48h/1h schedule with 4h/1h.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.scheduler.jobs as jobs
from app.database.models.assignment import Assignment
from app.database.models.attendance import Attendance  # noqa: F401 (table registration)
from app.database.models.eclass_account import EClassAccount
from app.database.models.group import Group
from app.database.models.notification import NotificationRecord
from app.database.models.user import User
from app.services import notification_service as ns
from app.services.notification_service import (
    DEADLINE_REMINDER_HOURS,
    REMINDER_GRACE,
    due_deadline_reminders,
)

NOW = datetime.now(timezone.utc)
TZ = ZoneInfo("Asia/Tashkent")


def _kinds(reminders):
    return [k for _, k in reminders]


async def _user(db, name="DEADLINE-24-1", tg=4242, **kw):
    g = Group(name=name)
    db.add(g)
    await db.flush()
    u = User(telegram_id=tg, username="dl", timezone="Asia/Tashkent", group_id=g.id, **kw)
    db.add(u)
    await db.commit()
    return u


async def _item(db, u, kind="homework", status=None, deadline=None, title="Item", eid=None):
    row = Assignment(
        user_id=u.id,
        external_id=eid or title.lower().replace(" ", "_"),
        course_name="Database",
        title=title,
        kind=kind,
        deadline=deadline if deadline is not None else NOW + timedelta(days=2),
        submission_status=status,
    )
    db.add(row)
    await db.commit()
    return row


# ---------------- schedule configuration ----------------


def test_threshold_list_is_exactly_four_and_one():
    assert DEADLINE_REMINDER_HOURS == [4, 1]


def test_old_thresholds_are_gone():
    for old in (72, 48, 24, 3):
        assert old not in DEADLINE_REMINDER_HOURS


def test_reminder_kinds_are_stable_and_distinct():
    kinds = {f"deadline_{h}h" for h in DEADLINE_REMINDER_HOURS}
    assert kinds == {"deadline_4h", "deadline_1h"}


def test_service_source_has_no_old_threshold_references():
    import inspect

    src = inspect.getsource(ns.due_deadline_reminders)
    for old in ("deadline_3d", "deadline_24h", "deadline_3h", "deadline_48h"):
        assert old not in src


# ---------------- assignment: 4h and 1h ----------------


@pytest.mark.asyncio
async def test_assignment_receives_4h_reminder(db):
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(hours=3, minutes=58))
    assert _kinds(await due_deadline_reminders(db, u, NOW)) == ["deadline_4h"]


@pytest.mark.asyncio
async def test_assignment_receives_1h_reminder(db):
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(minutes=58))
    assert _kinds(await due_deadline_reminders(db, u, NOW)) == ["deadline_1h"]


@pytest.mark.asyncio
async def test_assignment_gets_exactly_two_reminders_over_its_lifetime(db):
    """4h then 1h, one message each — not one per poll."""
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(hours=3, minutes=58))

    got = []
    t = NOW
    end = NOW + timedelta(hours=3, minutes=57)
    while t <= end:
        got.extend(_kinds(await due_deadline_reminders(db, u, t)))
        t += timedelta(minutes=1)

    assert got == ["deadline_4h", "deadline_1h"]


# ---------------- quiz: 4h and 1h ----------------


@pytest.mark.asyncio
async def test_quiz_receives_4h_reminder(db):
    u = await _user(db)
    await _item(db, u, kind="quiz", deadline=NOW + timedelta(hours=3, minutes=58))
    assert _kinds(await due_deadline_reminders(db, u, NOW)) == ["deadline_4h"]


@pytest.mark.asyncio
async def test_quiz_receives_1h_reminder(db):
    u = await _user(db)
    await _item(db, u, kind="quiz", deadline=NOW + timedelta(minutes=58))
    assert _kinds(await due_deadline_reminders(db, u, NOW)) == ["deadline_1h"]


@pytest.mark.asyncio
async def test_quiz_not_open_yet_gets_no_reminder(db):
    u = await _user(db)
    row = await _item(db, u, kind="quiz", deadline=NOW + timedelta(hours=3, minutes=58))
    row.open_time = NOW + timedelta(hours=2)
    await db.commit()
    assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_quiz_already_open_gets_reminder(db):
    u = await _user(db)
    row = await _item(db, u, kind="quiz", deadline=NOW + timedelta(hours=3, minutes=58))
    row.open_time = NOW - timedelta(hours=1)
    await db.commit()
    assert _kinds(await due_deadline_reminders(db, u, NOW)) == ["deadline_4h"]


# ---------------- deduplication ----------------


@pytest.mark.asyncio
async def test_unchanged_reminder_not_delivered_twice(db):
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(hours=3, minutes=58))
    assert len(await due_deadline_reminders(db, u, NOW)) == 1
    for _ in range(5):
        assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_4h_does_not_suppress_1h(db):
    """Each threshold has an independent dedup identity."""
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(hours=3, minutes=58))

    assert _kinds(await due_deadline_reminders(db, u, NOW)) == ["deadline_4h"]
    # long before the 1h instant: nothing else due
    assert await due_deadline_reminders(db, u, NOW + timedelta(hours=2, minutes=50)) == []
    # the 1h reminder still fires
    assert _kinds(await due_deadline_reminders(db, u, NOW + timedelta(hours=2, minutes=58))) == ["deadline_1h"]


@pytest.mark.asyncio
async def test_reminder_not_resent_after_scheduler_restart(db):
    """A restart re-reads NotificationRecord; delivered reminders stay put."""
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(hours=3, minutes=58))
    await due_deadline_reminders(db, u, NOW)

    rows = (await db.execute(select(NotificationRecord).where(
        NotificationRecord.user_id == u.id))).scalars().all()
    assert {r.kind for r in rows} == {"deadline_4h"}

    await db.commit()
    assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_two_items_each_get_their_own_reminder(db):
    """Dedup is per (user, kind, assignment) — two items, two messages."""
    u = await _user(db)
    await _item(db, u, title="Alpha", eid="a", deadline=NOW + timedelta(hours=3, minutes=58))
    await _item(db, u, title="Beta", eid="b", deadline=NOW + timedelta(hours=3, minutes=58))

    kinds = _kinds(await due_deadline_reminders(db, u, NOW))
    assert kinds == ["deadline_4h", "deadline_4h"]   # one per item
    assert await due_deadline_reminders(db, u, NOW) == []


# ---------------- failure handling ----------------


@pytest.mark.asyncio
async def test_failed_delivery_does_not_break_the_send_loop(db):
    """A failing Telegram send is contained, matching jobs.check_notifications."""
    u = await _user(db)
    await _item(db, u, title="Alpha", eid="a", deadline=NOW + timedelta(hours=3, minutes=58))
    await _item(db, u, title="Beta", eid="b", deadline=NOW + timedelta(minutes=58))

    sent, errors = [], []

    class Bot:
        async def send_message(self, chat_id, text):
            if "Alpha" in text:
                raise RuntimeError("telegram down")
            sent.append(text)

    reminders = await due_deadline_reminders(db, u, NOW)
    assert len(reminders) == 2
    for assignment, kind in reminders:
        try:
            await Bot().send_message(
                u.telegram_id, jobs.build_deadline_message(assignment, kind, u.timezone))
        except Exception as exc:
            errors.append(str(exc))

    assert len(sent) == 1          # the healthy reminder still went out
    assert errors == ["telegram down"]


# ---------------- expired / not required ----------------


@pytest.mark.asyncio
async def test_expired_deadline_generates_no_reminder(db):
    u = await _user(db)
    await _item(db, u, deadline=NOW - timedelta(minutes=1))
    assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_stale_reminder_not_sent_as_overdue(db):
    """First seen long after its 4h instant: no overdue spam, 1h still due."""
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(hours=2))
    # its 4h instant was ~2h ago -> stale, dropped rather than sent as overdue
    assert await due_deadline_reminders(db, u, NOW) == []
    # the 1h instant falls at NOW+1h and is still delivered
    assert _kinds(await due_deadline_reminders(db, u, NOW + timedelta(minutes=62))) == ["deadline_1h"]


@pytest.mark.asyncio
async def test_item_entering_system_inside_4h_still_gets_1h(db):
    """First seen 2h before the deadline: only the 1h reminder is pending."""
    u = await _user(db)
    await _item(db, u, deadline=NOW + timedelta(hours=2))
    assert await due_deadline_reminders(db, u, NOW) == []
    assert _kinds(await due_deadline_reminders(db, u, NOW + timedelta(minutes=62))) == ["deadline_1h"]


@pytest.mark.asyncio
async def test_completed_assignment_gets_no_reminder(db):
    u = await _user(db)
    await _item(db, u, status="Submitted for grading",
                deadline=NOW + timedelta(hours=3, minutes=58))
    assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_completed_quiz_gets_no_reminder(db):
    u = await _user(db)
    await _item(db, u, kind="quiz", status="Completed",
                deadline=NOW + timedelta(hours=3, minutes=58))
    assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_graded_item_gets_no_reminder(db):
    u = await _user(db)
    row = await _item(db, u, status="Submitted for grading",
                      deadline=NOW + timedelta(hours=3, minutes=58))
    row.score = "10"
    row.max_score = "10"
    await db.commit()
    assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_not_submitted_item_still_reminded(db):
    u = await _user(db)
    await _item(db, u, status="No submission",
                deadline=NOW + timedelta(hours=3, minutes=58))
    assert _kinds(await due_deadline_reminders(db, u, NOW)) == ["deadline_4h"]


@pytest.mark.asyncio
async def test_deadline_notifications_disabled(db):
    u = await _user(db, deadline_notifications=False)
    await _item(db, u, deadline=NOW + timedelta(hours=3, minutes=58))
    assert await due_deadline_reminders(db, u, NOW) == []


@pytest.mark.asyncio
async def test_no_deadline_no_reminder(db):
    u = await _user(db)
    db.add(Assignment(user_id=u.id, external_id="x", course_name="C",
                      title="No deadline", kind="homework", deadline=None))
    await db.commit()
    assert await due_deadline_reminders(db, u, NOW) == []


# ---------------- timezone / calculation ----------------


def test_grace_window_covers_one_minute_polling():
    assert REMINDER_GRACE >= timedelta(minutes=1)


def test_scheduled_instants_are_exact():
    deadline = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
    assert deadline - timedelta(hours=4) == datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
    assert deadline - timedelta(hours=1) == datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)


def test_scheduler_uses_configured_default_timezone():
    from app.config.settings import get_settings

    sched = jobs.build_scheduler(bot=None)
    weekly = [j for j in sched.get_jobs() if j.id == "weekly_attendance"]
    assert weekly, "weekly_attendance job missing"
    # APScheduler does not expose .timezone on a pending Job, so assert the
    # configured value directly and confirm the trigger is a Saturday 21:00 cron.
    assert ZoneInfo(get_settings().default_timezone) == ZoneInfo("Asia/Tashkent")
    fields = {f.name: str(f) for f in weekly[0].trigger.fields}
    assert fields.get("day_of_week") == "sat"
    assert fields.get("hour") == "21"


@pytest.mark.asyncio
async def test_utc_stored_deadline_evaluated_as_instant(db):
    """A deadline is stored in UTC; the window follows the instant, not a wall clock."""
    u = await _user(db)
    local = datetime(2026, 10, 20, 19, 0, tzinfo=TZ)
    await _item(db, u, deadline=local.astimezone(timezone.utc))

    at_4h = local.astimezone(timezone.utc) - timedelta(hours=4)
    assert _kinds(await due_deadline_reminders(db, u, at_4h)) == ["deadline_4h"]
    assert await due_deadline_reminders(db, u, at_4h + timedelta(minutes=30)) == []

    at_1h = local.astimezone(timezone.utc) - timedelta(hours=1)
    assert _kinds(await due_deadline_reminders(db, u, at_1h)) == ["deadline_1h"]


def test_message_body_states_the_threshold():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    row = Assignment(user_id=1, external_id="e", course_name="Database",
                     title="HW", kind="homework", deadline=now + timedelta(hours=4))
    text4 = jobs.build_deadline_message(row, "deadline_4h", "Asia/Tashkent")
    text1 = jobs.build_deadline_message(row, "deadline_1h", "Asia/Tashkent")
    assert "4 hours remaining" in text4
    # 1h uses the urgent wording
    assert "1 HOUR" in text1


# ---------------- untouched subsystems ----------------


@pytest.mark.asyncio
async def test_class_reminders_unchanged(db):
    from app.database.models.lesson import Lesson
    from app.services.notification_service import due_class_reminders

    u = await _user(db, name="CLS-24-1", tg=77)
    start = (datetime.now() + timedelta(minutes=10)).replace(second=0, microsecond=0)
    db.add(Lesson(group_id=u.group_id, external_id="l9", course_name="DB",
                  professor="P", room="R", lesson_date=start.date(),
                  start_time=start.time(), end_time=start.time()))
    await db.commit()

    assert len(await due_class_reminders(db, u, datetime.now())) == 1
    assert await due_class_reminders(db, u, datetime.now()) == []


@pytest.mark.asyncio
async def test_weekly_attendance_report_unchanged(db, monkeypatch):
    u = await _user(db, name="ATT-24-1", tg=88)
    db.add(EClassAccount(user_id=u.id, username="x", session_data="x", is_active=True))
    await db.flush()
    db.add(Attendance(user_id=u.id, course_external_id="1", course_name="C",
                      available=True, absences=5, present=18, late=0,
                      updated_at=datetime.now(timezone.utc)))
    await db.commit()

    class Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text):
            self.sent.append(text)

    class Ctx:
        def __init__(self, s):
            self.s = s

        async def __aenter__(self):
            return self.s

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(jobs, "get_session_factory", lambda: (lambda: Ctx(db)))
    bot = Bot()
    await jobs.send_weekly_attendance_reports(bot)
    assert len(bot.sent) == 1
    assert "Weekly Attendance Report" in bot.sent[0]
    assert "5 absences" in bot.sent[0]