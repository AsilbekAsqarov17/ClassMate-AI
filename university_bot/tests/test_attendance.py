"""Attendance: parsing, persistence, reporting and weekly-job behaviour."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database.models.attendance import Attendance, AttendanceRecord
from app.database.models.attendance_report_delivery import AttendanceReportDelivery
from app.eclass.attendance import looks_like_attendance_page, normalize_status, parse_attendance_report
from app.eclass.models import (
    STATUS_ABSENT,
    STATUS_EXCUSED,
    STATUS_LATE,
    STATUS_PRESENT,
    STATUS_UNKNOWN,
    EAttendance,
    EAttendanceResult,
)
from app.scheduler.jobs import build_scheduler
from app.services import attendance_service as svc

TODAY = date.today()


def _record(db, user, *, day, status, course="42", name="Computer Architecture",
            ext=None, raw=None, start="09:30"):
    r = AttendanceRecord(
        user_id=user.id,
        external_id=ext or f"{course}:{day.isoformat()}:{start}",
        course_external_id=course,
        course_name=name,
        session_date=day,
        start_time=datetime.strptime(start, "%H:%M").time(),
        status=status,
        raw_status=raw or status,
    )
    db.add(r)
    return r


# ---------------- status normalization ----------------
# HTML parsing against REAL E-Class responses lives in
# tests/test_attendance_parsing.py, driven by captured live fixtures.


def test_normalize_verified_column_names():
    assert normalize_status("Attendance") == STATUS_PRESENT
    assert normalize_status("Absence") == STATUS_ABSENT


def test_normalize_unrecognized_is_unknown_not_absent():
    """An unknown label must never be silently treated as absent or present."""
    assert normalize_status("PД±ndaniyo berildi") == STATUS_UNKNOWN
    assert normalize_status("???") == STATUS_UNKNOWN
    assert normalize_status(None) == STATUS_UNKNOWN
    assert normalize_status("") == STATUS_UNKNOWN


def test_normalize_keeps_excused_distinct_from_absent():
    assert normalize_status("Excused (medical)") != normalize_status("Absent")


# ---------------- summary maths ----------------


def test_percentage_uses_graded_sessions_only():
    s = svc.AttendanceSummary(present=3, absent=1)
    assert s.marked == 4
    assert s.percentage == 75.0


def test_percentage_is_none_when_nothing_graded():
    assert svc.AttendanceSummary().percentage is None
    # unknown sessions are NOT absences and must not create a fake rate
    assert svc.AttendanceSummary(unknown=5).percentage is None


def test_late_counts_as_attended():
    assert svc.AttendanceSummary(present=1, late=1).attended == 2


# ---------------- persistence ----------------


async def test_sync_stores_records_and_is_idempotent(db, user):
    result = EAttendanceResult(available=True, records=[
        EAttendance(external_id="42:2026-10-05", course_external_id="42",
                    course_name="CA", session_date=date(2026, 10, 5),
                    status=STATUS_ABSENT, raw_status="Absent"),
        EAttendance(external_id="42:2026-10-07", course_external_id="42",
                    course_name="CA", session_date=date(2026, 10, 7),
                    status=STATUS_PRESENT, raw_status="Present"),
    ])
    assert await svc.sync_attendance_records(db, user.id, result) == 2
    assert await svc.sync_attendance_records(db, user.id, result) == 2  # no duplicates
    rows = await svc.records_for_user(db, user.id)
    assert len(rows) == 2


async def test_unavailable_result_stores_nothing_and_reports_unavailable(db, user):
    result = EAttendanceResult(available=False, unavailable_reason="plugin not installed")
    await svc.sync_attendance_records(db, user.id, result)
    rows = await svc.records_for_user(db, user.id)
    assert rows == []


async def test_resync_updates_summaries_without_duplicate_rows(db, user):
    """Regression: re-syncing must upsert, not violate uq_attendance_user_course."""
    result = EAttendanceResult(
        available=True,
        records=[
            EAttendance(external_id="42:2026-10-05", course_external_id="42",
                        course_name="CA", session_date=date(2026, 10, 5),
                        status=STATUS_ABSENT, raw_status="absent"),
        ],
        empty_courses={"77": "Physics"},
    )
    await svc.sync_attendance_records(db, user.id, result)
    await svc.sync_attendance_records(db, user.id, result)  # must not raise

    summaries = (
        await db.execute(select(Attendance).where(Attendance.user_id == user.id))
    ).scalars().all()
    ids = sorted(s.course_external_id for s in summaries)
    assert ids == ["42", "77"]


async def test_empty_course_records_a_confirmed_zero(db, user):
    """A readable page with no sessions is a true zero, not missing data."""
    result = EAttendanceResult(
        available=True, records=[], empty_courses={"77": "Physics"}
    )
    await svc.sync_attendance_records(db, user.id, result)
    row = (
        await db.execute(
            select(Attendance).where(
                Attendance.user_id == user.id, Attendance.course_external_id == "77"
            )
        )
    ).scalar_one()
    assert row.available is True
    assert row.absences == 0
    assert row.present == 0


async def test_subject_report_distinguishes_confirmed_zero_from_missing(db, user):
    """A readable page with no sessions is a true zero, not missing data."""
    db.add(Attendance(user_id=user.id, course_external_id="77", course_name="Physics",
                      available=True, present=0, absences=0, late=0, excused=0,
                      updated_at=datetime.now(timezone.utc)))
    await db.commit()
    text = await svc._subject_report_text(db, user.id, "77")
    assert text == "📚 <b>Physics</b>\n\n✅ Absences: 0"


async def test_by_course_marks_unavailable_courses(db, user):
    db.add(Attendance(user_id=user.id, course_external_id="99", course_name="Physics",
                      available=False, updated_at=datetime.now(timezone.utc)))
    await db.commit()
    entries = await svc.by_course(db, user.id)
    assert entries["99"].available is False


# ---------------- week bounds ----------------


def test_week_bounds_monday_to_saturday():
    monday, saturday = svc.week_bounds(date(2026, 10, 14))  # Wednesday
    assert monday == date(2026, 10, 12)
    assert saturday == date(2026, 10, 17)


def test_week_bounds_from_monday_and_saturday():
    assert svc.week_bounds(date(2026, 10, 12)) == (date(2026, 10, 12), date(2026, 10, 17))
    assert svc.week_bounds(date(2026, 10, 17)) == (date(2026, 10, 12), date(2026, 10, 17))


def test_week_bounds_sunday_belongs_to_previous_week():
    assert svc.week_bounds(date(2026, 10, 18)) == (date(2026, 10, 12), date(2026, 10, 17))


def test_week_bounds_crosses_month_boundary():
    # 2026-10-26 is a Monday; the week runs Mon 26 Oct -> Sat 31 Oct.
    monday, saturday = svc.week_bounds(date(2026, 10, 28))
    assert monday == date(2026, 10, 26)
    assert saturday == date(2026, 10, 31)
    # The following Monday starts a new week.
    assert svc.week_bounds(date(2026, 11, 2)) == (date(2026, 11, 2), date(2026, 11, 7))


# ---------------- concise report formatting ----------------


def test_display_name_strips_course_code():
    assert svc.display_name("Operating System[202602-SOC3010-001]") == "Operating System"
    assert svc.display_name("Math") == "Math"


def test_overall_report_lists_every_subject_and_totals():
    entries = [("Operating System", 1), ("Database", 0), ("Computer Algorithm", 2)]
    text = svc.format_overall_report(entries)
    assert "- Operating System — 1 absence" in text
    assert "- Database — 0 absences" in text
    assert "- Computer Algorithm — 2 absences" in text
    assert "<b>Total absences: 3</b>" in text


def test_overall_report_marks_unavailable_instead_of_zero():
    text = svc.format_overall_report([("Database", None)])
    assert "unavailable" in text
    assert "- Database — 0 absences" not in text


def test_overall_report_has_no_periods_or_stats():
    text = svc.format_overall_report([("Database", 0)])
    for banned in ("Period", "Rate", "Last retrieved", "%", "Attended", "Late"):
        assert banned not in text


def test_weekly_report_is_scoped_and_concise():
    entries = [("Operating System", 1), ("Database", 0)]
    text = svc.format_weekly_report(entries)
    assert "Weekly Attendance Report" in text
    assert "Total absences this week: 1" in text
    assert "Period:" not in text


async def test_subject_report_is_minimal(db, user):
    monday, _ = svc.week_bounds()
    _record(db, user, day=monday, status=STATUS_ABSENT,
            name="Computer Architecture[202602-A-001]")
    db.add(Attendance(user_id=user.id, course_external_id="42",
                      course_name="Computer Architecture[202602-A-001]",
                      available=True, present=5, absences=1, late=0, excused=0,
                      updated_at=datetime.now(timezone.utc)))
    await db.commit()
    text = await svc._subject_report_text(db, user.id, "42")
    assert text == "📚 <b>Computer Architecture</b>\n\n❌ Absences: 1"


async def test_subject_report_shows_zero_when_data_confirmed(db, user):
    db.add(Attendance(user_id=user.id, course_external_id="42",
                      course_name="Database[202602-B-001]",
                      available=True, present=6, absences=0, late=0, excused=0,
                      updated_at=datetime.now(timezone.utc)))
    await db.commit()
    text = await svc._subject_report_text(db, user.id, "42")
    assert text == "📚 <b>Database</b>\n\n✅ Absences: 0"


async def test_subject_report_never_fakes_zero_when_unavailable(db, user):
    db.add(Attendance(user_id=user.id, course_external_id="42", course_name="Database",
                      available=False, updated_at=datetime.now(timezone.utc)))
    await db.commit()
    text = await svc._subject_report_text(db, user.id, "42")
    assert "unavailable" in text.lower()
    assert "Absences: 0" not in text


async def test_absence_lookup_does_not_double_count(db, user):
    """Re-synced rows share (user_id, external_id), so they cannot double count."""
    monday, _ = svc.week_bounds()
    _record(db, user, day=monday, status=STATUS_ABSENT)
    db.add(Attendance(user_id=user.id, course_external_id="42", course_name="CA",
                      available=True, present=1, absences=1, late=0, excused=0,
                      updated_at=datetime.now(timezone.utc)))
    await db.commit()
    absences, _, available = await svc._absence_for(db, user.id, "42")
    assert available is True
    assert absences == 1


async def test_absence_lookup_is_date_bounded(db, user):
    monday, saturday = svc.week_bounds()
    _record(db, user, day=monday, status=STATUS_ABSENT)
    _record(db, user, day=saturday - timedelta(days=8), status=STATUS_ABSENT)
    db.add(Attendance(user_id=user.id, course_external_id="42", course_name="CA",
                      available=True, present=1, absences=2, late=0, excused=0,
                      updated_at=datetime.now(timezone.utc)))
    await db.commit()
    scoped, _, _ = await svc._absence_for(db, user.id, "42", since=monday, until=saturday)
    assert scoped == 1  # only the in-week absence


# ---------------- delivery idempotency ----------------


async def test_unique_constraint_blocks_duplicate_weekly_delivery(db, user):
    from sqlalchemy.exc import IntegrityError

    monday, _ = svc.week_bounds()
    db.add(AttendanceReportDelivery(user_id=user.id, week_start=monday, status="sent"))
    await db.commit()
    db.add(AttendanceReportDelivery(user_id=user.id, week_start=monday, status="sent"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_sent_lookup_finds_previous_delivery(db, user):
    monday, _ = svc.week_bounds()
    db.add(AttendanceReportDelivery(user_id=user.id, week_start=monday, status="sent",
                                   delivered_at=datetime.now(timezone.utc)))
    await db.commit()
    found = (
        await db.execute(
            select(AttendanceReportDelivery).where(
                AttendanceReportDelivery.user_id == user.id,
                AttendanceReportDelivery.week_start == monday,
                AttendanceReportDelivery.status == "sent",
            )
        )
    ).scalar_one_or_none()
    assert found is not None


# ---------------- scheduler ----------------


def test_weekly_job_is_saturday_2100_asia_tashkent():
    job = next(j for j in build_scheduler(None).get_jobs() if j.id == "weekly_attendance_report")
    assert str(job.trigger) == "cron[day_of_week='sat', hour='21', minute='0']"
    assert str(job.trigger.timezone) == "Asia/Tashkent"


def test_weekly_job_does_not_build_a_backlog():
    job = next(j for j in build_scheduler(None).get_jobs() if j.id == "weekly_attendance_report")
    assert job.coalesce is True
    assert job.misfire_grace_time == 3600
    assert job.max_instances == 1


def test_existing_jobs_are_preserved():
    ids = {j.id for j in build_scheduler(None).get_jobs()}
    assert {"notifications", "sync", "weekly_attendance_report"} <= ids
