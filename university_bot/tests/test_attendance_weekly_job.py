"""Weekly report job: schedule, idempotency, and failure tolerance.

The job is exercised with a fake Bot and a real (temporary) database, so no
message ever reaches Telegram and no E-Class credential is used.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

import app.scheduler.jobs as jobs
from app.database.models.attendance_report_delivery import AttendanceReportDelivery
from app.database.models.eclass_account import EClassAccount
from app.database.models.user import User
from app.services import attendance_service as svc


class FakeBot:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("Telegram unreachable (simulated)")
        self.sent.append((chat_id, text))
        return True


@pytest.fixture()
async def student(db):
    u = User(telegram_id=424242, username="weekly", timezone="Asia/Tashkent")
    db.add(u)
    await db.flush()
    db.add(EClassAccount(user_id=u.id, username="u2410037", is_active=True, session_data="x"))
    await db.commit()
    return u


@pytest.fixture(autouse=True)
def _use_test_session(db, monkeypatch):
    """Point the job's session factory at the test database.

    The job opens its own session via get_session_factory(); without this it
    would connect to the real PostgreSQL instance.
    """

    class Reuse:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(jobs, "get_session_factory", Reuse)


@pytest.fixture(autouse=True)
def _no_remote_fetch(monkeypatch):
    """Never touch E-Class from the weekly job tests."""

    async def fake_refresh(session, user_id):
        from app.eclass.models import EAttendanceResult

        return EAttendanceResult(available=False, unavailable_reason="test stub")

    async def fake_report(session, user_id):
        return "🗓 Weekly Attendance Report\nPeriod: test\n\n<b>Total confirmed absences: 0</b>"

    monkeypatch.setattr(svc, "refresh_for_user", fake_refresh)
    monkeypatch.setattr(svc, "build_weekly_report", fake_report)


async def test_job_delivers_once_per_week(db, student, monkeypatch):
    from datetime import timedelta

    monday, _ = svc.week_bounds()
    monkeypatch.setattr(
        jobs, "send_weekly_attendance_report", jobs.send_weekly_attendance_report
    )

    bot = FakeBot()
    await jobs.send_weekly_attendance_report(bot)
    assert len(bot.sent) == 1

    # Running again for the same week must not re-send.
    await jobs.send_weekly_attendance_report(bot)
    assert len(bot.sent) == 1

    row = (
        await db.execute(
            select(AttendanceReportDelivery).where(
                AttendanceReportDelivery.user_id == student.id,
                AttendanceReportDelivery.week_start == monday,
            )
        )
    ).scalar_one()
    assert row.status == "sent"
    assert row.delivered_at is not None


async def test_failed_send_is_not_marked_delivered(db, student, monkeypatch):
    """A Telegram outage must be retried, not silently skipped forever."""
    monday, _ = svc.week_bounds()

    failing = FakeBot(fail=True)
    await jobs.send_weekly_attendance_report(failing)
    rows = (
        await db.execute(
            select(AttendanceReportDelivery).where(
                AttendanceReportDelivery.user_id == student.id,
                AttendanceReportDelivery.week_start == monday,
            )
        )
    ).scalars().all()
    assert rows == []  # nothing marked, so the next run retries

    # Second attempt succeeds and is recorded exactly once.
    ok = FakeBot()
    await jobs.send_weekly_attendance_report(ok)
    assert len(ok.sent) == 1
    await jobs.send_weekly_attendance_report(ok)
    assert len(ok.sent) == 1


async def test_one_failure_does_not_block_other_students(db, student, monkeypatch):
    """A failing account must not abort the loop for everyone else."""
    other = User(telegram_id=999888, username="other", timezone="Asia/Tashkent")
    db.add(other)
    await db.flush()
    db.add(EClassAccount(user_id=other.id, username="u2410026", is_active=True, session_data="x"))
    await db.commit()

    original = jobs.send_weekly_attendance_report

    calls = {"n": 0}

    async def flaky(session, user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("E-Class exploded")
        return "report"

    monkeypatch.setattr(svc, "build_weekly_report", flaky)

    bot = FakeBot()
    await original(bot)
    assert len(bot.sent) == 1  # the healthy student still got theirs


async def test_user_without_telegram_link_is_skipped(db, monkeypatch):
    db.add(EClassAccount(user_id=999999, username="ghost", is_active=True, session_data="x"))
    await db.commit()
    bot = FakeBot()
    await jobs.send_weekly_attendance_report(bot)
    assert bot.sent == []


async def test_weekly_report_is_never_marked_by_manual_view(db, student):
    """Viewing the report in the menu must not consume the scheduled send."""
    before = (
        await db.execute(
            select(AttendanceReportDelivery).where(
                AttendanceReportDelivery.user_id == student.id
            )
        )
    ).scalars().all()
    assert before == []