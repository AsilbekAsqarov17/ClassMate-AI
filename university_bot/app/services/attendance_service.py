"""Attendance: E-Class is the source of truth, fetched on demand.

Three states must stay distinct:
  * Attendance exists          → CourseAttendance.available=True, real numbers
  * Blank/unrecorded section   → available=False, "Attendance not available"
  * E-Class/network/session    → SessionExpiredError / EClassUnavailableError
    failure                      (never converted to 0 absences / unavailable)
"""

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.attendance import Attendance
from app.database.models.eclass_account import EClassAccount
from app.eclass.models import CourseAttendance
from app.services.eclass_session import client_for_account

UNAVAILABLE_NOTE = (
    "Your professor may not be recording attendance through E-Class."
)

# Shown when a course IS tracked by E-Class but the page reported no absence
# count. Distinct from "0 absences" and from a fully blank attendance section.
ABSENCE_UNREPORTED = (
    "Absences: not reported by E-Class for this course yet."
)


async def sync_attendance(
    session: AsyncSession, account: EClassAccount, client, synced_at: datetime
) -> bool:
    """Fetch live attendance via the production E-Class path and persist it.

    Persists only after a successful parser run. On any failure the caller
    keeps the previously persisted rows untouched.
    """
    items: list[CourseAttendance] = await client.get_attendance()
    for ca in items:
        row = (
            await session.execute(
                select(Attendance).where(
                    Attendance.user_id == account.user_id,
                    Attendance.course_external_id == ca.course_external_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = Attendance(
                user_id=account.user_id,
                course_external_id=ca.course_external_id,
                course_name=ca.course_name,
                available=ca.available,
                updated_at=synced_at,
            )
            session.add(row)
        row.course_name = ca.course_name
        row.available = ca.available
        row.absences = ca.absences if ca.available else None
        row.present = ca.present if ca.available else None
        row.late = ca.late if ca.available else None
        row.excused = ca.excused if ca.available else None
        row.records_json = (
            json.dumps([{"date": r.date, "status": r.status} for r in ca.records])
            if ca.available
            else None
        )
        row.updated_at = synced_at
    return True


async def get_persisted(session: AsyncSession, user_id: int) -> list[Attendance]:
    return list(
        (
            await session.execute(
                select(Attendance)
                .where(Attendance.user_id == user_id)
                .order_by(Attendance.course_name)
            )
        ).scalars()
    )


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def attendance_is_stale(account: EClassAccount | None, rows: list[Attendance]) -> bool:
    """True when the last E-Class sync attempt did not refresh attendance."""
    if account is None:
        return False
    if not account.is_active:
        return bool(rows) or account.last_sync is not None
    if account.last_sync is None:
        return False  # never synced: nothing to be stale against
    if not rows:
        return True
    newest = max(_as_utc(r.updated_at) for r in rows)
    return newest < _as_utc(account.last_sync)


def format_last_updated(dt: datetime, tzname: str | None) -> str:
    from zoneinfo import ZoneInfo

    from app.config.settings import get_settings

    tz = ZoneInfo(tzname or get_settings().default_timezone)
    local = _as_utc(dt).astimezone(tz)
    return f"{local.day} {local:%b} {local:%Y}, {local:%H:%M}"


async def fetch_attendance_for_account(
    session: AsyncSession, account: EClassAccount
) -> list[CourseAttendance]:
    client = await client_for_account(session, account)
    try:
        return await client.get_attendance()
    finally:
        await client.aclose()


async def fetch_attendance_for_user(
    session: AsyncSession, user_id: int
) -> list[CourseAttendance]:
    account = (
        await session.execute(select(EClassAccount).where(EClassAccount.user_id == user_id))
    ).scalar_one_or_none()
    if account is None or not account.is_active:
        raise LookupError("No active E-Class account")
    return await fetch_attendance_for_account(session, account)


STALE_WARNING = (
    "⚠️ Unable to update attendance from E-Class.\n"
    "Showing the last successfully synchronized data."
)


def format_attendance_row(row: Attendance, tzname: str | None) -> str:
    lines: list[str] = []
    if not row.available:
        lines.append(
            f"ℹ️ Attendance data is not available for {row.course_name} in E-Class.\n\n"
            f"{UNAVAILABLE_NOTE}"
        )
    else:
        lines.append(f"📚 {row.course_name}\n")
        if row.absences is not None:
            lines.append(f"Absences: {row.absences}")
        else:
            lines.append(ABSENCE_UNREPORTED)
        if row.late is not None:
            lines.append(f"Late: {row.late}")
        if row.present is not None:
            lines.append(f"Present: {row.present}")
        if row.excused is not None:
            lines.append(f"Excused: {row.excused}")
        recs = row.records()
        if recs:
            lines.append("\nRecent attendance:")
            for r in recs[:10]:
                lines.append(f"• {r['date']} — {r['status']}")
    lines.append(f"\nLast updated: {format_last_updated(row.updated_at, tzname)}")
    return "\n".join(lines)


def build_weekly_report_from_rows(
    rows: list[Attendance], stale: bool, tzname: str | None
) -> str:
    lines = ["📊 Weekly Attendance Report\n"]
    for row in rows:
        if not row.available:
            lines.append(f"{row.course_name} — Attendance not available in E-Class")
        elif row.absences is not None:
            n = row.absences
            lines.append(f"{row.course_name} — {n} absence{'s' if n != 1 else ''}")
        else:
            lines.append(f"{row.course_name} — absence count not reported by E-Class")
    if stale and rows:
        newest = max(r.updated_at for r in rows)
        lines.append(
            f"\n⚠️ E-Class could not be reached for the latest update. "
            f"Last successful sync: {format_last_updated(newest, tzname)}"
        )
    return "\n".join(lines)


def format_course_attendance(ca: CourseAttendance) -> str:
    if not ca.available:
        return (
            f"ℹ️ Attendance data is not available for {ca.course_name} in E-Class.\n\n"
            f"{UNAVAILABLE_NOTE}"
        )
    lines = [f"📚 {ca.course_name}\n"]
    if ca.absences is not None:
        lines.append(f"Absences: {ca.absences}")
    else:
        lines.append(ABSENCE_UNREPORTED)
    if ca.late is not None:
        lines.append(f"Late: {ca.late}")
    if ca.excused is not None:
        lines.append(f"Excused: {ca.excused}")
    if ca.present is not None:
        lines.append(f"Present: {ca.present}")
    if ca.records:
        lines.append("\nRecent attendance:")
        for r in ca.records[:10]:
            lines.append(f"• {r.date} — {r.status}")
    return "\n".join(lines)


def build_weekly_report(items: list[CourseAttendance]) -> str:
    lines = ["📊 Weekly Attendance Report\n"]
    for ca in items:
        if not ca.available:
            lines.append(f"{ca.course_name} — Attendance not available in E-Class")
        elif ca.absences is not None:
            n = ca.absences
            lines.append(f"{ca.course_name} — {n} absence{'s' if n != 1 else ''}")
        else:
            lines.append(f"{ca.course_name} — absence count not reported by E-Class")
    return "\n".join(lines)
