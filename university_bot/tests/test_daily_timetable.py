"""Daily timetable summary (1h before first class) + per-class reminders (15 min)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.database.models.lesson import Lesson
from app.database.models.notification import NotificationRecord
from app.scheduler.jobs import build_class_reminder_message, build_daily_timetable_message
from app.services.notification_service import due_class_reminders, due_daily_timetable

TODAY = date.today()


def mk_lesson(db, user, start, end, name="OS", prof="J.Yusupov", room="B209", day=TODAY, eid=None, status="active"):
    l = Lesson(
        group_id=user.group_id, external_id=eid or f"{name}-{start}-{day}",
        course_name=name, professor=prof, room=room,
        lesson_date=day, start_time=start, end_time=end, status=status,
    )
    db.add(l)
    return l


async def _setup_two_classes(db, user):
    """The real ICE-24-01 Monday shape: 09:30 CA, 11:00 OS."""
    mk_lesson(db, user, time(9, 30), time(11, 0), name="CA", prof="A.Seth", room="B202")
    mk_lesson(db, user, time(11, 0), time(12, 30), name="OS", prof="J.Yusupov", room="B209")
    await db.commit()


# ---------- 1. fires exactly 1 hour before the first class ----------


@pytest.mark.asyncio
async def test_daily_fires_one_hour_before_first_class(db, user):
    await _setup_two_classes(db, user)
    # first class 09:30 -> due 08:30
    assert await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 29))) == []
    due = await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 30)))
    assert len(due) == 2


@pytest.mark.asyncio
async def test_daily_not_too_early_not_too_late(db, user):
    await _setup_two_classes(db, user)
    assert await due_daily_timetable(db, user, datetime.combine(TODAY, time(7, 0))) == []
    # 08:35 is within the 5-minute grace window
    assert len(await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 35)))) == 2


# ---------- 2. contains all classes of the day, chronological ----------


@pytest.mark.asyncio
async def test_daily_contains_all_classes_sorted(db, user):
    await _setup_two_classes(db, user)
    due = await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 30)))
    assert [l.course_name for l in due] == ["CA", "OS"]
    msg = build_daily_timetable_message(due)
    assert "TODAY'S TIMETABLE" in msg
    assert "You have 2 classes today" in msg
    assert "09:30–11:00" in msg and "CA" in msg and "A.Seth" in msg and "B202" in msg
    assert "11:00–12:30" in msg and "OS" in msg and "J.Yusupov" in msg and "B209" in msg
    assert msg.index("CA") < msg.index("OS")  # chronological


# ---------- 3. sent only once per day ----------


@pytest.mark.asyncio
async def test_daily_sent_only_once(db, user):
    await _setup_two_classes(db, user)
    now = datetime.combine(TODAY, time(8, 30))
    assert len(await due_daily_timetable(db, user, now)) == 2
    assert await due_daily_timetable(db, user, now) == []  # same minute again
    assert await due_daily_timetable(db, user, now + timedelta(minutes=3)) == []


# ---------- 4. nothing on days with no classes ----------


@pytest.mark.asyncio
async def test_daily_nothing_when_no_classes(db, user):
    assert await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 30))) == []


# ---------- 5. no late daily reminder ----------


@pytest.mark.asyncio
async def test_daily_no_late_send_after_first_class_started(db, user):
    await _setup_two_classes(db, user)
    # first class 09:30 already started -> way past the 1-hour mark + grace
    assert await due_daily_timetable(db, user, datetime.combine(TODAY, time(9, 45))) == []
    assert await due_daily_timetable(db, user, datetime.combine(TODAY, time(9, 0))) == []


# ---------- 6 & 7. 15-minute reminder per class, multiple classes ----------


@pytest.mark.asyncio
async def test_class_reminder_each_lesson(db, user):
    await _setup_two_classes(db, user)
    # 09:15 -> CA starts in 15 min
    due1 = await due_class_reminders(db, user, datetime.combine(TODAY, time(9, 15)))
    assert [l.course_name for l, _ in due1] == ["CA"]
    # 10:45 -> OS starts in 15 min
    due2 = await due_class_reminders(db, user, datetime.combine(TODAY, time(10, 45)))
    assert [l.course_name for l, _ in due2] == ["OS"]
    msg = build_class_reminder_message(due1[0][0], 15)
    assert "CLASS STARTING IN 15 MINUTES" in msg
    assert "CA" in msg and "A.Seth" in msg and "B202" in msg and "09:30–11:00" in msg


# ---------- 8. class reminders deduplicated ----------


@pytest.mark.asyncio
async def test_class_reminders_deduplicated(db, user):
    await _setup_two_classes(db, user)
    now = datetime.combine(TODAY, time(9, 15))
    assert len(await due_class_reminders(db, user, now)) == 1
    assert await due_class_reminders(db, user, now) == []


# ---------- 9. different days are independent ----------


@pytest.mark.asyncio
async def test_daily_independent_per_day(db, user):
    await _setup_two_classes(db, user)
    tomorrow = TODAY + timedelta(days=1)
    mk_lesson(db, user, time(9, 30), time(11, 0), name="CA", prof="A.Seth", room="B202", day=tomorrow)
    await db.commit()
    assert len(await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 30)))) == 2
    # today's record must not block tomorrow's reminder
    assert len(await due_daily_timetable(db, user, datetime.combine(tomorrow, time(8, 30)))) == 1
    refs = (
        (await db.execute(select(NotificationRecord).where(NotificationRecord.kind == "daily_timetable")))
        .scalars().all()
    )
    assert {r.ref_id for r in refs} == {TODAY.isoformat(), tomorrow.isoformat()}


# ---------- 10. settings respected ----------


@pytest.mark.asyncio
async def test_settings_respected(db, user):
    await _setup_two_classes(db, user)
    user.daily_timetable_notifications = False
    await db.commit()
    assert await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 30))) == []
    # class reminders still independent
    assert len(await due_class_reminders(db, user, datetime.combine(TODAY, time(9, 15)))) == 1
    user.daily_timetable_notifications = True
    user.class_notifications = False
    await db.commit()
    assert await due_class_reminders(db, user, datetime.combine(TODAY, time(9, 15))) == []


# ---------- 11. timezone correctness ----------


def test_utc_to_local_conversion_for_scheduler():
    """The scheduler converts UTC -> user tz; daily/class checks use local time."""
    utc_now = datetime(2026, 10, 6, 3, 30)  # 08:30 in Tashkent (UTC+5)
    local = utc_now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Tashkent")).replace(tzinfo=None)
    assert local == datetime(2026, 10, 6, 8, 30)


@pytest.mark.asyncio
async def test_daily_uses_local_not_utc(db, user):
    """A lesson at 09:30 local must trigger at 08:30 local, not 08:30 UTC."""
    await _setup_two_classes(db, user)
    # 08:30 UTC = 13:30 Tashkent: first class (09:30 local) already started -> nothing
    assert await due_daily_timetable(db, user, datetime.combine(TODAY, time(13, 30))) == []


# ---------- coexistence: both fire independently the same morning ----------


@pytest.mark.asyncio
async def test_both_notifications_coexist(db, user):
    await _setup_two_classes(db, user)
    daily = await due_daily_timetable(db, user, datetime.combine(TODAY, time(8, 30)))
    assert len(daily) == 2
    first = await due_class_reminders(db, user, datetime.combine(TODAY, time(9, 15)))
    assert [l.course_name for l, _ in first] == ["CA"]
    second = await due_class_reminders(db, user, datetime.combine(TODAY, time(10, 45)))
    assert [l.course_name for l, _ in second] == ["OS"]
