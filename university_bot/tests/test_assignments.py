from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.assignment import Assignment
from app.services.assignment_service import overdue, remaining_text, upcoming


def test_remaining_text_days():
    dl = datetime.utcnow() + timedelta(days=2, hours=6)
    assert "2 days" in remaining_text(dl)


def test_remaining_text_hours():
    dl = datetime.utcnow() + timedelta(hours=3)
    assert "hours remaining" in remaining_text(dl)


def test_remaining_text_overdue():
    assert remaining_text(datetime.utcnow() - timedelta(days=1)) == "Overdue"


@pytest.mark.asyncio
async def test_upcoming_orders_by_deadline(db: AsyncSession, user):
    db.add_all([
        Assignment(user_id=user.id, external_id="a1", course_name="C", title="T1", deadline=datetime.utcnow() + timedelta(days=5)),
        Assignment(user_id=user.id, external_id="a2", course_name="C", title="T2", deadline=datetime.utcnow() + timedelta(days=1)),
    ])
    await db.commit()
    items = await upcoming(db, user.id)
    assert [a.external_id for a in items] == ["a2", "a1"]


@pytest.mark.asyncio
async def test_overdue(db: AsyncSession, user):
    db.add(Assignment(user_id=user.id, external_id="x", course_name="C", title="T", deadline=datetime.utcnow() - timedelta(days=1)))
    await db.commit()
    assert len(await overdue(db, user.id)) == 1
