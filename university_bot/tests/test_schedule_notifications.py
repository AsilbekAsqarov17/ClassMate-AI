from datetime import date, time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.lesson import Lesson
from app.services.notification_service import due_class_reminders
from app.services.schedule_service import lessons_on, next_lesson


@pytest.mark.asyncio
async def test_lessons_on_returns_todays_lessons(db: AsyncSession, user, lesson):
    items = await lessons_on(db, user.id, date.today())
    assert len(items) == 1
    assert items[0].course_name == "Data Structures"


@pytest.mark.asyncio
async def test_empty_schedule(db: AsyncSession, user):
    assert await lessons_on(db, user.id, date(2020, 1, 1)) == []


@pytest.mark.asyncio
async def test_cancelled_lesson_hidden(db: AsyncSession, user, lesson):
    lesson.status = "cancelled"
    await db.commit()
    assert await lessons_on(db, user.id, date.today()) == []


@pytest.mark.asyncio
async def test_class_reminder_fires_once(db: AsyncSession, user):
    from datetime import datetime

    start = (datetime.now() + __import__("datetime").timedelta(minutes=10)).replace(second=0, microsecond=0)
    lesson = Lesson(
        group_id=user.group_id, external_id="l2", course_name="DB", professor="Kim", room="D-210",
        lesson_date=start.date(), start_time=start.time(),
        end_time=(start + __import__("datetime").timedelta(minutes=75)).time(),
    )
    db.add(lesson)
    await db.commit()

    reminders = await due_class_reminders(db, user, datetime.now())
    assert len(reminders) == 1
    # second call must not resend
    assert await due_class_reminders(db, user, datetime.now()) == []


@pytest.mark.asyncio
async def test_disabled_notifications(db: AsyncSession, user, lesson):
    user.class_notifications = False
    await db.commit()
    assert await due_class_reminders(db, user, __import__("datetime").datetime.now()) == []
