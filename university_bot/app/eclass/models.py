"""E-Class-agnostic data models used across the app."""

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Course:
    external_id: str
    name: str
    professor: str | None = None
    kind: str | None = None


@dataclass
class Lesson:
    external_id: str
    course_external_id: str
    course_name: str
    date: date
    start_time: str  # "HH:MM"
    end_time: str    # "HH:MM"
    room: str | None = None
    building: str | None = None
    professor: str | None = None
    lesson_type: str | None = None


@dataclass
class Assignment:
    external_id: str
    course_external_id: str
    course_name: str
    title: str
    deadline: datetime | None = None
    submission_status: str | None = None
    description: str | None = None
    submission_url: str | None = None
    kind: str = "homework"
    score: str | None = None
    max_score: str | None = None
    open_time: datetime | None = None
    time_limit: str | None = None
    attempts_allowed: str | None = None


@dataclass
class SyncResult:
    courses: list[Course] = field(default_factory=list)
    lessons: list[Lesson] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)


# ---------- attendance ----------

# Normalized status vocabulary. E-Class (Moodle) returns free-text, possibly
# localized labels; every raw value is mapped onto exactly one of these so the
# rest of the app never has to guess. STATUS_UNKNOWN is a deliberate bucket:
# a session whose label we do not recognize must never be silently counted as
# either present or absent.
STATUS_PRESENT = "present"
STATUS_ABSENT = "absent"
STATUS_LATE = "late"
STATUS_EXCUSED = "excused"
STATUS_UNKNOWN = "unknown"


@dataclass
class EAttendance:
    """One attendance session as reported by E-Class for a single student.

    `raw_status` is preserved verbatim so an unrecognized localized label can be
    re-examined later instead of being lost.
    """

    external_id: str
    course_external_id: str
    course_name: str
    session_date: date
    status: str = STATUS_UNKNOWN
    start_time: str | None = None   # "HH:MM"
    end_time: str | None = None     # "HH:MM"
    raw_status: str | None = None


@dataclass
class EAttendanceResult:
    """Outcome of an attendance fetch.

    `available=False` means E-Class exposed no attendance data at all — which is
    materially different from "fetched successfully, zero absences". Callers
    must never present the former as the latter.

    `empty_courses` maps course_external_id -> course_name for courses whose page
    WAS readable but recorded no session yet, so callers can report a confirmed
    zero instead of claiming the data is missing.
    """

    available: bool = False
    records: list[EAttendance] = field(default_factory=list)
    unavailable_reason: str | None = None
    empty_courses: dict[str, str] = field(default_factory=dict)
