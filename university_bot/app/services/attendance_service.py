"""Attendance history: E-Class sessions -> DB, plus per-course statistics.

Attendance is a growing history rather than a watchlist, so rows are never
deleted just because they stopped being returned; only the retention window in
`sync_attendance_records` prunes them.

Central rule: a course is either `available=True` (we have real records) or
`available=False` (we could not read it). The two are never collapsed into
"0 absences".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.attendance import Attendance, AttendanceRecord
from app.database.models.eclass_account import EClassAccount
from app.eclass.attendance import normalize_status
from app.eclass.client import EClassError, EClassUnavailableError
from app.eclass.models import (
    STATUS_ABSENT,
    STATUS_EXCUSED,
    STATUS_LATE,
    STATUS_PRESENT,
    STATUS_UNKNOWN,
    EAttendance,
    EAttendanceResult,
)

# Attendance records older than this are dropped on sync (keeps the table from
# growing forever while preserving a full semester of history).
ATTENDANCE_RETENTION_DAYS = 400

# Statuses that represent a real verdict. `unknown` is deliberately excluded:
# an unrecognized label must not inflate either the attended or absent count.
_MARKED_STATUSES = (STATUS_PRESENT, STATUS_ABSENT, STATUS_LATE, STATUS_EXCUSED)
_ATTENDED_STATUSES = (STATUS_PRESENT, STATUS_LATE)

_STATUS_EMOJI = {
    STATUS_PRESENT: "✅",
    STATUS_ABSENT: "❌",
    STATUS_LATE: "⏰",
    STATUS_EXCUSED: "📄",
    STATUS_UNKNOWN: "❓",
}

_STATUS_FIELDS = ("present", "absent", "late", "excused")


@dataclass
class AttendanceSummary:
    """Counters for a set of attendance records (one course, or everything)."""

    present: int = 0
    absent: int = 0
    late: int = 0
    excused: int = 0
    unknown: int = 0

    def add(self, status: str) -> None:
        if status == STATUS_PRESENT:
            self.present += 1
        elif status == STATUS_ABSENT:
            self.absent += 1
        elif status == STATUS_LATE:
            self.late += 1
        elif status == STATUS_EXCUSED:
            self.excused += 1
        else:
            self.unknown += 1

    @property
    def marked(self) -> int:
        """Sessions carrying a real verdict (excused/unknown are not graded)."""
        return sum(getattr(self, f) for f in _STATUS_FIELDS)

    @property
    def attended(self) -> int:
        """Sessions the student actually made it to (present + late)."""
        return self.present + self.late

    @property
    def percentage(self) -> float | None:
        """Attendance rate over graded sessions, or None if nothing is graded.

        Unknown/excused sessions are excluded from the denominator rather than
        being counted as absences.
        """
        graded = self.marked
        if graded == 0:
            return None
        return round(self.attended / graded * 100, 1)


@dataclass
class CourseAttendance:
    """One course's availability plus its counters and recent records."""

    course_external_id: str
    course_name: str
    available: bool = False
    summary: AttendanceSummary = field(default_factory=AttendanceSummary)
    records: list[AttendanceRecord] = field(default_factory=list)
    reason: str | None = None

    def percentage(self) -> float | None:
        """Attendance rate for this course, or None when nothing is graded."""
        return self.summary.percentage


def _to_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def summarize(records: list[AttendanceRecord | EAttendance]) -> AttendanceSummary:
    """Tally a list of records into an AttendanceSummary."""
    out = AttendanceSummary()
    for r in records:
        out.add(getattr(r, "status", STATUS_UNKNOWN))
    return out


def status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, _STATUS_EMOJI[STATUS_UNKNOWN])


async def sync_attendance_records(
    session: AsyncSession,
    user_id: int,
    result: EAttendanceResult,
) -> int:
    """Persist attendance, upserting by (user_id, external_id).

    Re-syncing the same session is a no-op, so this is safe to call on every
    scheduler run and on manual refresh. Returns the number of stored records.
    """
    cutoff = date.today() - timedelta(days=ATTENDANCE_RETENTION_DAYS)
    existing = {
        r.external_id: r
        for r in (
            await session.execute(
                select(AttendanceRecord).where(AttendanceRecord.user_id == user_id)
            )
        ).scalars()
    }

    if result.available:
        for item in result.records:
            row = existing.get(item.external_id)
            if row is None:
                row = AttendanceRecord(
                    user_id=user_id,
                    external_id=item.external_id,
                    course_external_id=item.course_external_id,
                    course_name=item.course_name,
                    session_date=item.session_date,
                    status=item.status,
                    raw_status=item.raw_status,
                )
                session.add(row)
                existing[item.external_id] = row
            else:
                row.course_name = item.course_name
                row.status = item.status
                row.raw_status = item.raw_status
            row.start_time = _to_time(item.start_time)
            row.end_time = _to_time(item.end_time)
        await session.flush()

        # Prune only what fell outside the retention window.
        stale = [
            r.external_id
            for r in existing.values()
            if r.session_date < cutoff
        ]
        if stale:
            await session.execute(
                delete(AttendanceRecord).where(
                    AttendanceRecord.user_id == user_id,
                    AttendanceRecord.external_id.in_(stale),
                )
            )

        rows = list(existing.values())
        by_course: dict[str, list[AttendanceRecord]] = {}
        for r in rows:
            if r.session_date >= cutoff:
                by_course.setdefault(r.course_external_id or "", []).append(r)

        # Reuse existing summary rows so a re-sync updates them instead of
        # violating uq_attendance_user_course.
        summaries = {
            s.course_external_id: s
            for s in (
                await session.execute(
                    select(Attendance).where(Attendance.user_id == user_id)
                )
            ).scalars()
        }

        for course_id, recs in by_course.items():
            summ = summarize(recs)
            summary_row = summaries.get(course_id)
            if summary_row is None:
                summary_row = Attendance(
                    user_id=user_id,
                    course_external_id=course_id,
                    course_name=recs[0].course_name,
                )
                session.add(summary_row)
                summaries[course_id] = summary_row
            summary_row.available = True
            summary_row.present = summ.present
            summary_row.absences = summ.absent
            summary_row.late = summ.late
            summary_row.excused = summ.excused
            summary_row.records_json = json.dumps(
                [
                    {
                        "date": r.session_date.isoformat(),
                        "status": r.status,
                        "raw_status": r.raw_status,
                    }
                    for r in recs
                ],
                ensure_ascii=False,
            )
            summary_row.updated_at = datetime.now(timezone.utc)

        # Record courses E-Class answered for but which have no recorded
        # session yet, so a confirmed zero stays distinguishable from a page
        # that could not be read at all.
        for course_id, course_label in (result.empty_courses or {}).items():
            if course_id in by_course:
                continue
            empty_row = summaries.get(course_id)
            if empty_row is None:
                empty_row = Attendance(
                    user_id=user_id,
                    course_external_id=course_id,
                    course_name=course_label,
                )
                session.add(empty_row)
                summaries[course_id] = empty_row
            empty_row.available = True
            empty_row.present = 0
            empty_row.absences = 0
            empty_row.late = 0
            empty_row.excused = 0
            empty_row.records_json = None
            empty_row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return len(existing)


async def records_for_user(
    session: AsyncSession,
    user_id: int,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
) -> list[AttendanceRecord]:
    """Attendance records for a student, newest first, optionally date-bounded."""
    q = select(AttendanceRecord).where(AttendanceRecord.user_id == user_id)
    if since is not None:
        q = q.where(AttendanceRecord.session_date >= since)
    if until is not None:
        q = q.where(AttendanceRecord.session_date <= until)
    q = q.order_by(AttendanceRecord.session_date.desc(), AttendanceRecord.start_time.desc())
    if limit is not None:
        q = q.limit(limit)
    return list((await session.execute(q)).scalars())


async def by_course(
    session: AsyncSession,
    user_id: int,
    since: date | None = None,
    until: date | None = None,
) -> dict[str, CourseAttendance]:
    """Per-course counters, weakest attendance first so problems surface."""
    out: dict[str, CourseAttendance] = {}
    for r in await records_for_user(session, user_id, since, until):
        key = r.course_external_id or r.course_name
        entry = out.setdefault(
            key,
            CourseAttendance(course_external_id=key, course_name=r.course_name),
        )
        entry.records.append(r)
    for entry in out.values():
        entry.available = True
        entry.summary = summarize(entry.records)
        entry.records.sort(key=lambda r: r.session_date, reverse=True)

    # Courses explicitly marked unavailable (so "no data" shows up too).
    summaries = (
        await session.execute(
            select(Attendance).where(
                Attendance.user_id == user_id, Attendance.available.is_(False)
            )
        )
    ).scalars()
    for s in summaries:
        out.setdefault(
            s.course_external_id,
            CourseAttendance(
                course_external_id=s.course_external_id,
                course_name=s.course_name,
                available=False,
                reason="E-Class did not return attendance for this course",
            ),
        )

    def sort_key(item: tuple[str, CourseAttendance]) -> tuple[bool, float]:
        _, entry = item
        pct = entry.percentage()
        # Weakest attendance first; unavailable courses sink to the bottom.
        return (pct is None, pct if pct is not None else 0.0)

    return dict(sorted(out.items(), key=sort_key))


def format_status_line(r: AttendanceRecord) -> str:
    when = f"{r.session_date:%B %d}"
    if r.start_time:
        when += f" — {r.start_time:%H:%M}"
    return f"{status_emoji(r.status)} {when} — {r.status.capitalize()}"


def format_record(r: AttendanceRecord) -> str:
    """One absence line: date, session label, and E-Class's own status word."""
    label = _status_word(r)
    when = f"{r.session_date:%Y-%m-%d}"
    session = _session_label(r)
    if session:
        when += f" ({session})"
    return f"  • {when} — {label}"


def _status_word(r: AttendanceRecord) -> str:
    """Human-readable status. Falls back to the stored normalized value."""
    raw = (r.raw_status or "").strip()
    if raw and raw.lower() not in ("present", "absent", "late", "excused", "unknown"):
        return raw  # a localized/extra label worth showing verbatim
    return (r.raw_status or r.status or "unknown").capitalize()


def _session_label(r: AttendanceRecord) -> str:
    """E-Class labels sessions '1Class', '2Class'; recover it from external_id."""
    parts = (r.external_id or "").split(":")
    tail = parts[-1] if parts else ""
    return tail if tail.lower().endswith("class") else ""


def _tally_line(summary: AttendanceSummary) -> str:
    pct = summary.percentage
    pct_text = f"{pct}%" if pct is not None else "n/a"
    return (
        f"✅ Attended: {summary.attended}   "
        f"❌ Absent: {summary.absent}   "
        f"⏰ Late: {summary.late}   "
        f"📄 Excused: {summary.excused}   "
        f"📈 Rate: {pct_text}"
    )


def format_attendance_report(
    entries: dict[str, CourseAttendance],
    title: str = "📊 MY ATTENDANCE",
    period: str | None = None,
) -> str:
    """Render the per-course breakdown used by /attendance and the weekly job."""
    lines = [title, ""]
    if period:
        lines += [f"Period: {period}", ""]
    if not entries:
        lines.append("🎉 No attendance records yet.")
        return "\n".join(lines)

    for entry in entries.values():
        if not entry.available:
            lines.append(f"❔ {entry.course_name} — attendance unavailable")
            continue
        lines.append(f"📚 {entry.course_name}")
        lines.append(f"   {_tally_line(entry.summary)}")
        absences = [r for r in entry.records if r.status == STATUS_ABSENT]
        if absences:
            lines.append("   Absence records:")
            for r in absences:
                lines.append(format_record(r))
        lines.append("")
    return "\n".join(lines).strip()


# ---------- report period ----------

UNAVAILABLE_TEXT = (
    "⚠️ E-Class did not provide attendance data for your account.\n\n"
    "{reason}\n\n"
    "This is not the same as having zero absences — nothing could be read."
)

_DEFAULT_TZ = "Asia/Tashkent"


def week_bounds(ref: date | None = None, tzname: str | None = None) -> tuple[date, date]:
    """Monday..Saturday of the academic week containing `ref`.

    The report covers Monday through Saturday inclusive, matching the Saturday
    21:00 send. Weeks start on Monday (weekday 0).
    """
    tz = ZoneInfo(tzname or _DEFAULT_TZ)
    today = ref or datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=5)  # Saturday


async def refresh_for_user(session: AsyncSession, user_id: int) -> EAttendanceResult:
    """Pull the latest attendance from E-Class for one student and store it."""
    from app.services.eclass_session import SessionExpiredError, client_for_account

    account = (
        await session.execute(
            select(EClassAccount).where(
                EClassAccount.user_id == user_id, EClassAccount.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if account is None:
        return EAttendanceResult(available=False, unavailable_reason="No E-Class account linked")

    client = None
    try:
        client = await client_for_account(session, account)
        result = await client.get_attendance()
    except SessionExpiredError:
        return EAttendanceResult(
            available=False, unavailable_reason="E-Class session expired — reconnect with /start"
        )
    except (EClassUnavailableError, EClassError) as exc:
        return EAttendanceResult(available=False, unavailable_reason=f"E-Class unreachable: {exc}")
    finally:
        if client is not None:
            await client.aclose()

    await sync_attendance_records(session, user_id, result)
    return result


async def list_subjects(session: AsyncSession, user_id: int) -> list[tuple[str, str]]:
    """Subjects to offer, from live E-Class courses, falling back to stored rows."""
    from app.services.eclass_session import SessionExpiredError, client_for_account

    courses: list[tuple[str, str]] = []
    account = (
        await session.execute(
            select(EClassAccount).where(
                EClassAccount.user_id == user_id, EClassAccount.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if account is not None:
        client = None
        try:
            client = await client_for_account(session, account)
            courses = sorted(
                (c.external_id, c.name) for c in await client.get_courses()
            )
        except (SessionExpiredError, EClassError):
            log.info("attendance: live course list unavailable for user %s", user_id)
        finally:
            if client is not None:
                await client.aclose()

    if courses:
        return courses
    rows = (
        await session.execute(
            select(Attendance).where(Attendance.user_id == user_id).order_by(Attendance.course_name)
        )
    ).scalars()
    return [(r.course_external_id, r.course_name) for r in rows]


async def subject_report_text(session: AsyncSession, user_id: int, course_id: str) -> str:
    """Minimal per-subject report: name + confirmed absence count.

    Deliberately terse - no periods, rates, percentages or timestamps.
    """
    await refresh_for_user(session, user_id)
    return await _subject_report_text(session, user_id, course_id)


def display_name(course_name: str) -> str:
    """Readable course name: drop E-Class's trailing "[202602-XXX-001]" code."""
    return re.sub(r"\[[^\]]*\]\s*$", "", course_name).strip() or course_name.strip()


async def _subject_report_text(session: AsyncSession, user_id: int, course_id: str) -> str:
    """Render one subject's absence count from stored data (no network).

    Absence count is `None` when the page could not be read, which renders as
    "unavailable" rather than a misleading 0.
    """
    absences, name, available = await _absence_for(session, user_id, course_id)
    title = f"📚 <b>{display_name(name)}</b>"
    if not available:
        return f"{title}\n\n⚠️ Attendance data unavailable"
    if absences == 0:
        return f"{title}\n\n✅ Absences: 0"
    return f"{title}\n\n❌ Absences: {absences}"


async def _absence_for(
    session: AsyncSession,
    user_id: int,
    course_id: str,
    since: date | None = None,
    until: date | None = None,
) -> tuple[int | None, str, bool]:
    """(confirmed absences, course name, data available) for one course."""
    summary_row = (
        await session.execute(
            select(Attendance).where(
                Attendance.user_id == user_id,
                Attendance.course_external_id == course_id,
            )
        )
    ).scalar_one_or_none()

    records = await records_for_user(session, user_id, since=since, until=until)
    mine = [r for r in records if r.course_external_id == course_id]

    name = (
        summary_row.course_name
        if summary_row
        else (mine[0].course_name if mine else course_id)
    )
    available = summary_row.available if summary_row else bool(mine)
    if not available:
        return None, name, False
    # Records are unique per session, so counting them cannot double-count.
    return sum(1 for r in mine if r.status == STATUS_ABSENT), name, True


async def all_subject_absences(
    session: AsyncSession,
    user_id: int,
    since: date | None = None,
    until: date | None = None,
) -> list[tuple[str, int | None]]:
    """(display name, confirmed absences or None if unavailable) per subject.

    Iterates the live E-Class course list so every enrolled subject appears,
    including those with no recorded sessions.
    """
    out: list[tuple[str, int | None]] = []
    for course_id, name in await list_subjects(session, user_id):
        absences, _, available = await _absence_for(
            session, user_id, course_id, since=since, until=until
        )
        out.append((display_name(name), absences if available else None))
    return out


def _absence_phrase(count: int) -> str:
    return f"{count} absence" if count == 1 else f"{count} absences"


def format_overall_report(entries: list[tuple[str, int | None]]) -> str:
    """Compact per-subject absence list plus the total."""
    lines = ["📊 <b>Overall Attendance Report</b>", ""]
    total = 0
    for name, absences in entries:
        if absences is None:
            # Never render unreadable data as zero.
            lines.append(f"- {name} — ⚠️ unavailable")
        else:
            lines.append(f"- {name} — {_absence_phrase(absences)}")
            total += absences
    lines += ["", f"<b>Total absences: {total}</b>"]
    return "\n".join(lines)


def format_weekly_report(entries: list[tuple[str, int | None]]) -> str:
    """Same concise shape, scoped to the reporting week."""
    lines = ["📊 <b>Weekly Attendance Report</b>", ""]
    total = 0
    for name, absences in entries:
        if absences is None:
            lines.append(f"- {name} — ⚠️ unavailable")
        else:
            lines.append(f"- {name} — {_absence_phrase(absences)}")
            total += absences
    lines += ["", f"<b>Total absences this week: {total}</b>"]
    return "\n".join(lines)


async def build_weekly_report(session: AsyncSession, user_id: int) -> str:
    """Monday..Saturday report for one student (after a fresh fetch).

    Scoped to the reporting week only - NOT semester totals.
    """
    monday, saturday = week_bounds()
    entries = await all_subject_absences(session, user_id, since=monday, until=saturday)
    return format_weekly_report(entries)


async def manual_overall_text(session: AsyncSession, user_id: int) -> str:
    """On-demand overall report from the menu (never recorded as delivered)."""
    await refresh_for_user(session, user_id)
    return format_overall_report(await all_subject_absences(session, user_id))


def _period_label(records: list[AttendanceRecord]) -> str:
    dates = sorted({r.session_date for r in records})
    if len(dates) == 1:
        return f"{dates[0]:%B %d, %Y}"
    return f"{dates[0]:%B %d} – {dates[-1]:%B %d, %Y}"