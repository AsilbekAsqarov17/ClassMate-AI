"""Registration allowlist: only approved Student IDs may connect."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.allowed_student import AllowedStudent
from app.services.allowlist_service import allowlist_size, is_allowed


async def _seed(db: AsyncSession, *ids: str) -> None:
    for sid in ids:
        db.add(AllowedStudent(student_id=sid))
    await db.commit()


async def test_listed_id_is_allowed(db: AsyncSession):
    await _seed(db, "u2410037", "u2410026")
    assert await is_allowed(db, "u2410037") is True
    assert await is_allowed(db, "u2410026") is True


async def test_unlisted_id_is_denied(db: AsyncSession):
    await _seed(db, "u2410037")
    assert await is_allowed(db, "u2410099") is False


async def test_matching_is_case_insensitive(db: AsyncSession):
    """A human may type U2410037 while the row is stored lowercase."""
    await _seed(db, "u2410037")
    assert await is_allowed(db, "U2410037") is True
    assert await is_allowed(db, "  u2410037  ") is True


async def test_surrounding_whitespace_is_tolerated(db: AsyncSession):
    await _seed(db, "u2410037")
    assert await is_allowed(db, " u2410037\n") is True


async def test_empty_allowlist_denies_everyone(db: AsyncSession):
    """Fail closed: a missing table must not silently open registration."""
    assert await is_allowed(db, "u2410037") is False


async def test_empty_allowlist_logs_an_error(db: AsyncSession, caplog):
    import logging

    with caplog.at_level(logging.ERROR, logger="app.services.allowlist_service"):
        assert await is_allowed(db, "u2410037") is False
    assert any("allowed_student_ids is empty" in r.message for r in caplog.records)


async def test_denied_id_does_not_poison_later_lookups(db: AsyncSession):
    await _seed(db, "u2410026")
    assert await is_allowed(db, "u2410099") is False
    assert await is_allowed(db, "u2410026") is True


async def test_allowlist_size(db: AsyncSession):
    assert await allowlist_size(db) == 0
    await _seed(db, "u2410026", "u2410027")
    assert await allowlist_size(db) == 2


async def test_stored_ids_are_unique(db: AsyncSession):
    from sqlalchemy.exc import IntegrityError

    await _seed(db, "u2410037")
    db.add(AllowedStudent(student_id="u2410037"))
    with pytest.raises(IntegrityError):
        await db.commit()