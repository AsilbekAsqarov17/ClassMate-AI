"""Periodic sync: EClass courses/assignments/quizzes → DB; EduPage lessons → DB."""

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.assignment import Assignment
from app.database.models.course import Course
from app.database.models.eclass_account import EClassAccount
from app.database.models.lesson import Lesson
from app.config.settings import get_settings
from app.eclass.client import EClassError, EClassUnavailableError
from app.edupage.client import EduPageError
from app.edupage.web_client import EduPageWebClient
from app.eclass.models import Assignment as EAssignment
from app.eclass.models import Course as ECourse
from app.services.eclass_session import SessionExpiredError, client_for_account


class SyncOutcome:
    def __init__(self) -> None:
        self.courses_ok = False
        self.timetable_ok = False
        self.assignments_ok = False
        self.quizzes_ok = False
        self.new_scores: list[Assignment] = []
        self.session_expired = False
        self.error: str | None = None
        self.attendance_ok = False


async def sync_user(session: AsyncSession, account: EClassAccount) -> SyncOutcome:
    outcome = SyncOutcome()
    try:
        client = await client_for_account(session, account)
    except SessionExpiredError as exc:
        # Genuine expiration: mark the account so the scheduler stops using it
        # and the student is asked to reconnect (password is never stored, so
        # no silent re-login is possible).
        account.is_active = False
        await session.commit()
        outcome.session_expired = True
        outcome.error = str(exc)
        return outcome
    except EClassUnavailableError as exc:
        # transient network problem: account stays active, no reconnect prompt
        outcome.error = str(exc)
        return outcome

    try:
        # courses
        try:
            courses: list[ECourse] = await client.get_courses()
            if not courses:
                raise EClassError("E-Class returned 0 courses — session likely invalid; keeping old data")
            await session.execute(delete(Course).where(Course.user_id == account.user_id))
            for c in courses:
                session.add(Course(user_id=account.user_id, external_id=c.external_id,
                                   name=c.name, professor=c.professor, kind=c.kind))
            outcome.courses_ok = True
        except Exception as exc:
            outcome.error = f"courses: {exc}"

        # assignments + quizzes
        try:
            assignments: list[EAssignment] = await client.get_assignments()
            quizzes: list[EAssignment] = await client.get_quizzes()
            if not assignments and not quizzes:
                raise EClassError("E-Class returned 0 assignments/quizzes — session likely invalid; keeping old data")
            from app.services.academic_sync import sync_academic_records

            outcome.new_scores = await sync_academic_records(session, account.user_id, assignments + quizzes)
            outcome.assignments_ok = True
            outcome.quizzes_ok = True
        except Exception as exc:
            outcome.error = f"assignments: {exc}"

        # timetable from EduPage (shared per group)
        try:
            edupage = EduPageWebClient()
            from app.database.models.group import Group
            from app.database.models.user import User

            user = (await session.execute(select(User).where(User.id == account.user_id))).scalar_one()
            if user.group_id is None:
                raise EduPageError("No study group set for this user")
            group = (await session.execute(select(Group).where(Group.id == user.group_id))).scalar_one()
            now_utc = datetime.now(__import__("datetime").timezone.utc)
            today_ = now_utc.astimezone(__import__("zoneinfo").ZoneInfo(get_settings().default_timezone)).date()
            taken = await edupage.get_timetable(group.name, today_, today_ + timedelta(days=30))
            await session.execute(
                delete(Lesson).where(Lesson.group_id == group.id, Lesson.lesson_date >= today_)
            )
            for l in taken:
                session.add(Lesson(
                    group_id=group.id, external_id=l.external_id, course_name=l.course_name,
                    professor=l.professor, room=l.room, lesson_date=l.date,
                    start_time=datetime.strptime(l.start_time, "%H:%M").time(),
                    end_time=datetime.strptime(l.end_time, "%H:%M").time(),
                ))
            outcome.timetable_ok = True
        except Exception as exc:
            outcome.error = (outcome.error + "; " if outcome.error else "") + f"timetable: {exc}"

        # attendance (persisted; failures keep the previously saved data)
        now_sync = datetime.now(__import__("datetime").timezone.utc)
        try:
            from app.services.attendance_service import sync_attendance

            await sync_attendance(session, account, client, now_sync)
            outcome.attendance_ok = True
        except Exception as exc:
            # do NOT overwrite persisted attendance with anything; just report
            outcome.error = (outcome.error + "; " if outcome.error else "") + f"attendance: {exc}"

        account.last_sync = now_sync
        await session.commit()
        return outcome
    finally:
        await client.aclose()  # release the HTTP connection pool after each sync
