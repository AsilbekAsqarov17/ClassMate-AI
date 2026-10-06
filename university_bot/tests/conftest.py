import asyncio
from datetime import date, time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models.base import Base
from app.database.models.group import Group
from app.database.models.lesson import Lesson
from app.database.models.user import User


@pytest.fixture()
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture()
async def user(db: AsyncSession) -> User:
    g = Group(name="CSE-23-1")
    db.add(g)
    await db.flush()
    u = User(telegram_id=12345, username="test", timezone="Asia/Tashkent", group_id=g.id)
    db.add(u)
    await db.commit()
    return u


@pytest.fixture()
async def lesson(db: AsyncSession, user: User) -> Lesson:
    l = Lesson(
        group_id=user.group_id, external_id="l1", course_name="Data Structures",
        professor="Prof. Smith", room="B-204", lesson_date=date.today(),
        start_time=time(14, 0), end_time=time(15, 15),
    )
    db.add(l)
    await db.commit()
    return l
