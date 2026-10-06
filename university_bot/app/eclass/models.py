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
