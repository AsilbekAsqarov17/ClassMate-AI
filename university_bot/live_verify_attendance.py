"""TEMPORARY manual live verification for the attendance feature.

Runs the PRODUCTION attendance code against the real authenticated E-Class
account stored in the local database. Safe by design:

  * reuses the existing encrypted session mechanism (no password, ever)
  * prints only course-level diagnostics — never cookies, session_data,
    Fernet keys, tokens, or raw HTML
  * does NOT write to the database
  * does NOT send any Telegram messages

Usage (from the university_bot/ directory):

    python live_verify_attendance.py                # first active account
    python live_verify_attendance.py <telegram_id>  # specific user

Remove this file after live verification is done — it is not imported by
the bot, the scheduler, or the test suite, so deleting it is safe.
"""

import asyncio
import re
import sys

import httpx
from sqlalchemy import select

from app.database.database import get_session_factory
from app.database.models.eclass_account import EClassAccount
from app.database.models.user import User
from app.eclass.client import EClassAuthError, EClassUnavailableError
from app.services.attendance_service import UNAVAILABLE_NOTE  # noqa: F401 (context)
from app.services.eclass_session import SessionExpiredError, client_for_account


def classify(exc: Exception) -> str:
    if isinstance(exc, (SessionExpiredError, EClassAuthError)):
        return "SESSION ERROR (reconnect with /start)"
    if isinstance(exc, (EClassUnavailableError, httpx.HTTPError)):
        return "REQUEST/NETWORK ERROR"
    return f"ERROR ({type(exc).__name__})"


async def main() -> None:
    tg = sys.argv[1] if len(sys.argv) > 1 else None
    async with get_session_factory()() as session:
        q = select(EClassAccount).where(EClassAccount.is_active.is_(True))
        if tg is not None:
            u = (await session.execute(select(User).where(User.telegram_id == int(tg)))).scalar_one_or_none()
            if u is None:
                print(f"No user with telegram_id={tg}")
                return
            q = q.where(EClassAccount.user_id == u.id)
        account = (await session.execute(q)).scalars().first()
        if account is None:
            print("No active E-Class account found.")
            return

        try:
            client = await client_for_account(session, account)
        except Exception as exc:
            print(f"Could not establish authenticated session: {classify(exc)}")
            return

        print(f"E-Class account OK (username={account.username!r}). Fetching courses…\n")
        try:
            courses = await client.get_courses()
            print(f"{len(courses)} course(s) found.\n")
            for course in courses:
                print(f"• {course.name}")
                # 1) does an attendance activity exist on the course page?
                try:
                    html = await client._get_text(f"/course/view.php?id={course.external_id}")
                    found_activity = bool(re.findall(r'href="[^"]*attendance[^"]*"', html, re.I))
                except Exception as exc:
                    print(f"    attendance activity : <unknown> ({classify(exc)})")
                    continue
                print(f"    attendance activity : {'found' if found_activity else 'NOT found'}")
                # 2) run the production parser/service for this course
                try:
                    ca = await client.get_course_attendance(course)
                except Exception as exc:
                    print(f"    attendance data     : <skipped> ({classify(exc)})")
                    continue
                if ca.available:
                    print("    attendance data     : AVAILABLE")
                    print(f"    absences={ca.absences} present={ca.present} late={ca.late} excused={ca.excused}")
                    print(f"    parsed records      : {len(ca.records)}")
                else:
                    print("    attendance data     : genuinely blank/unrecorded in E-Class")
                print()
        except Exception as exc:
            print(f"Listing courses failed: {classify(exc)}")
        finally:
            await client.aclose()
    print("Verification finished. No database writes, no Telegram messages sent.")


if __name__ == "__main__":
    asyncio.run(main())
