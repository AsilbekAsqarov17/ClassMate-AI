"""E-Class attendance parsing for the `local_ubattendance` plugin.

VERIFIED against the live E-Class instance (eclass.inha.ac.kr, Moodle,
theme `coursemosv2`) using the project's stored authenticated session for
u2410037 on 2026-10-11.

Where attendance actually lives
-------------------------------
Attendance is NOT a Moodle activity. It is served by a university-specific
local plugin, reachable per course from the course page's "Grade/Attendance"
menu:

    /local/ubattendance/my_status.php?id=<courseid>

Confirmed by the course page markup:
    <a href=".../local/ubattendance/my_status.php?id=2607"
       title="Offline-Attendance" class="submenu-attendance">

Verified response structure
--------------------------
A `<table summary="Attendance status" class="attendance_my ...">` whose
`<tbody>` holds one row per session:

    <tr><td class="week_name text-center">2026-09-08</td>
        <td class="text-center">1Class</td>
        <td class="text-center">○</td>          <!-- Attendance -->
        <td class="text-center">&nbsp;</td>     <!-- Absence     -->
        <td class="text-center">&nbsp;</td>     <!-- Late        -->
        <td class="text-center">&nbsp;</td>     <!-- Others      -->
    </tr>

Columns 3-6 are mutually exclusive: exactly one holds the marker `○` for a
recorded session, all others are `&nbsp;`. A session with no marker at all is
NOT recorded yet (verified: future sessions after today show all-empty cells)
and must never be counted as an absence.

The `<tfoot>` repeats server-computed totals, which this parser reproduces
exactly (verified course-by-course against the live site).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape

from app.eclass.models import (
    STATUS_ABSENT,
    STATUS_LATE,
    STATUS_PRESENT,
    STATUS_UNKNOWN,
    EAttendance,
)

__all__ = [
    "normalize_status",
    "parse_attendance_report",
    "looks_like_attendance_page",
    "SUMMARY_PRESENT",
    "SUMMARY_ABSENT",
    "SUMMARY_LATE",
    "SUMMARY_OTHERS",
]

# The marker the plugin puts in exactly one status column per recorded session.
MARKER = "○"

# <tfoot> order is fixed: Attendance, Absence, Late, Others.
SUMMARY_PRESENT = "present"
SUMMARY_ABSENT = "absent"
SUMMARY_LATE = "late"
SUMMARY_OTHERS = "others"
_SUMMARY_FIELDS = (SUMMARY_PRESENT, SUMMARY_ABSENT, SUMMARY_LATE, SUMMARY_OTHERS)

_ROW_RE = re.compile(
    r'<tr>\s*<td class="week_name text-center">(.*?)</td>(.*?)</tr>', re.S | re.I
)
_CELL_RE = re.compile(r'<td class="text-center">(.*?)</td>', re.S | re.I)
_TFOOT_RE = re.compile(
    r"<strong>Attendance</strong>\s*:\s*(\d+).*?"
    r"<strong>Absence</strong>\s*:\s*(\d+).*?"
    r"<strong>Late</strong>\s*:\s*(\d+).*?"
    r"<strong>Others</strong>\s*:\s*(\d+)",
    re.S | re.I,
)


def _text(fragment: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", fragment)).replace("\xa0", " ").strip()


def normalize_status(column: str | None) -> str:
    """Map a status-column index/label onto the normalized vocabulary.

    Accepts the column header ("Attendance"/"Absence"/"Late"/"Others") or a
    status code. Anything unrecognized maps to STATUS_UNKNOWN so it is never
    silently reported as present or absent.
    """
    if column is None:
        return STATUS_UNKNOWN
    value = str(column).strip().lower()
    if value in ("attendance", "present", "attended"):
        return STATUS_PRESENT
    if value in ("absence", "absent", "absences"):
        return STATUS_ABSENT
    if value in ("late",):
        return STATUS_LATE
    return STATUS_UNKNOWN


def looks_like_attendance_page(html: str) -> bool:
    """True when the page is the ubattendance status page (not a 404/login)."""
    if len(html.strip()) < 500:
        return False
    low = html.lower()
    return "attendance_my" in low or "attendance status" in low


def parse_summary(html: str) -> dict[str, int] | None:
    """Server-computed totals from the table footer, or None if absent."""
    m = _TFOOT_RE.search(html)
    if not m:
        return None
    return {name: int(value) for name, value in zip(_SUMMARY_FIELDS, m.groups())}


def parse_attendance_report(
    html: str,
    course_external_id: str,
    course_name: str,
) -> list[EAttendance] | None:
    """Parse the `attendance_my` table for one course.

    Returns the recorded sessions, or ``None`` when the page is not an
    attendance page. An empty list is a valid result: it means the course
    genuinely has no recorded sessions yet, which is different from the page
    being unreadable.

    Sessions with no marker in any status column are skipped entirely - E-Class
    lists scheduled-but-unrecorded sessions with empty cells, and counting
    those as absences would fabricate data.
    """
    if not looks_like_attendance_page(html):
        return None

    parsed: list[EAttendance] = []
    for row in _ROW_RE.finditer(html):
        raw_date = _text(row.group(1))
        rest = row.group(2)
        try:
            session_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue

        cells = [_text(c) for c in _CELL_RE.findall(rest)]
        # cells[0] is the class label ("1Class"); the next four are the
        # Attendance/Absence/Late/Others status columns.
        class_label = cells[0] if cells else ""
        markers = cells[1:5]
        if len(markers) < 4:
            continue

        status, marked_col = STATUS_UNKNOWN, None
        for idx, column in enumerate(_SUMMARY_FIELDS):
            if markers[idx] == MARKER:
                status = normalize_status(column)
                marked_col = column
                break
        if marked_col is None:
            continue  # unrecorded session: never an absence

        parsed.append(
            EAttendance(
                # One row per (course, date, class label): stable and unique.
                external_id=f"{course_external_id}:{session_date.isoformat()}:{class_label}",
                course_external_id=course_external_id,
                course_name=course_name,
                session_date=session_date,
                status=status,
                # The plugin exposes no start/end times on this page.
                start_time=None,
                end_time=None,
                raw_status=marked_col,
            )
        )

    parsed.sort(key=lambda r: (r.session_date, r.external_id))
    return parsed