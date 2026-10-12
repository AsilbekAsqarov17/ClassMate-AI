"""Attendance parsing against REAL E-Class responses.

The fixtures in tests/fixtures/ubattendance.json were captured verbatim from
the live `local_ubattendance` pages on 2026-10-11 using the stored session for
u2410037, so these tests assert against the university's actual HTML rather
than a guessed shape.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.eclass.attendance import (
    looks_like_attendance_page,
    normalize_status,
    parse_attendance_report,
    parse_summary,
)
from app.eclass.models import STATUS_ABSENT, STATUS_PRESENT, STATUS_UNKNOWN

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "ubattendance.json").read_text(encoding="utf-8")
)


# ---------------- status normalization ----------------


def test_normalize_verified_column_names():
    assert normalize_status("Attendance") == STATUS_PRESENT
    assert normalize_status("Absence") == STATUS_ABSENT
    assert normalize_status("Late") == "late"
    assert normalize_status("Others") == STATUS_UNKNOWN


def test_normalize_unknown_is_never_absent():
    assert normalize_status("particip") == STATUS_UNKNOWN
    assert normalize_status(None) == STATUS_UNKNOWN
    assert normalize_status("") == STATUS_UNKNOWN


# ---------------- page detection ----------------


def test_real_pages_are_recognised():
    for name in ("with_absences", "with_unrecorded", "empty_course"):
        assert looks_like_attendance_page(FIXTURES[name]) is True


def test_404_and_login_pages_are_rejected():
    assert looks_like_attendance_page("<html><body>404</body></html>") is False
    assert parse_attendance_report("<html>404 not found</html>", "1", "C") is None


# ---------------- the real absences course ----------------


def test_parses_real_absence_dates():
    rows = parse_attendance_report(FIXTURES["with_absences"], "2581", "Engineering Communications")
    assert rows is not None
    absent = sorted(r.session_date for r in rows if r.status == STATUS_ABSENT)
    # Absences verified on the live site for course 2581.
    assert absent == [date(2026, 9, 17), date(2026, 9, 29), date(2026, 10, 8)]


def test_reproduces_server_totals_for_real_course():
    """The parser must agree with E-Class's own footer totals."""
    rows = parse_attendance_report(FIXTURES["with_absences"], "2581", "Eng")
    summary = parse_summary(FIXTURES["with_absences"])
    assert summary is not None
    assert summary["present"] == sum(1 for r in rows if r.status == STATUS_PRESENT)
    assert summary["absent"] == sum(1 for r in rows if r.status == STATUS_ABSENT)
    assert summary == {"present": 6, "absent": 3, "late": 0, "others": 0}


def test_real_records_carry_course_identity_and_raw_status():
    rows = parse_attendance_report(FIXTURES["with_absences"], "2581", "Engineering Communications")
    assert all(r.course_external_id == "2581" for r in rows)
    assert all(r.course_name == "Engineering Communications" for r in rows)
    assert all(r.raw_status in ("present", "absent") for r in rows)


def test_external_ids_are_unique_per_session():
    rows = parse_attendance_report(FIXTURES["with_absences"], "2581", "Eng")
    ids = [r.external_id for r in rows]
    assert len(ids) == len(set(ids))


def test_rows_are_chronological():
    rows = parse_attendance_report(FIXTURES["with_absences"], "2581", "Eng")
    dates = [r.session_date for r in rows]
    assert dates == sorted(dates)


# ---------------- unrecorded sessions ----------------


def test_unrecorded_sessions_are_excluded_not_counted_absent():
    """Course 2528 lists scheduled-but-unrecorded sessions with empty cells.

    Those must not become absences - that would fabricate data.
    """
    rows = parse_attendance_report(FIXTURES["with_unrecorded"], "2528", "Electronic Circuit")
    assert rows is not None
    assert all(r.status != STATUS_ABSENT for r in rows)
    summary = parse_summary(FIXTURES["with_unrecorded"])
    assert summary == {"present": 10, "absent": 0, "late": 0, "others": 0}
    # 14 sessions are listed but only 10 carry a marker.
    assert len(rows) == 10


def test_future_sessions_beyond_today_are_not_included():
    rows = parse_attendance_report(FIXTURES["with_unrecorded"], "2528", "Electronic Circuit")
    today = date(2026, 10, 11)  # capture date
    assert all(r.session_date < today for r in rows)


# ---------------- empty course ----------------


def test_course_with_no_recorded_sessions_returns_empty_not_none():
    rows = parse_attendance_report(FIXTURES["empty_course"], "2533", "Signals and Systems")
    assert rows == []  # readable, genuinely empty - NOT "unavailable"
    assert parse_summary(FIXTURES["empty_course"]) == {
        "present": 0, "absent": 0, "late": 0, "others": 0,
    }


def test_missing_summary_returns_none():
    assert parse_summary("<table>no footer</table>") is None


# ---------------- robustness ----------------


def test_malformed_rows_are_skipped_not_crashed():
    broken = FIXTURES["with_absences"].replace("2026-09-17", "not-a-date")
    rows = parse_attendance_report(broken, "2581", "Eng")
    assert rows is not None
    assert date(2026, 9, 17) not in {r.session_date for r in rows}