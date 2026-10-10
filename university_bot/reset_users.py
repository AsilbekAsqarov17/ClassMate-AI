"""One-off local maintenance: remove all registered bot users and their
user-owned data, preserving application-wide data.

Safety properties:
  * operates ONLY on the configured DATABASE_URL; refuses if it does not
    look like a local development database
  * deletes nothing outside an explicit allowlist of user-owned tables
  * preserves allowed_student_ids, groups, lessons, alembic_version
  * single transaction with an explicit FK-safe deletion order
  * prints counts only, never secrets or session material

Usage:  python reset_users.py            # dry run, prints the plan
        python reset_users.py --execute  # performs the deletion
"""
import asyncio
import sys

import sqlalchemy as sa
from sqlalchemy import func, select

from app.database.database import get_engine, get_session_factory
from app.database.models.allowed_student_id import AllowedStudentID
from app.database.models.assignment import Assignment
from app.database.models.attendance import Attendance
from app.database.models.course import Course
from app.database.models.eclass_account import EClassAccount
from app.database.models.group import Group
from app.database.models.lesson import Lesson
from app.database.models.notification import NotificationRecord
from app.database.models.user import User

# Deleted — children first so no FK is ever dangling mid-transaction.
DELETE_ORDER = [
    ("notifications", NotificationRecord),
    ("attendance", Attendance),
    ("assignments", Assignment),
    ("courses", Course),
    ("eclass_accounts", EClassAccount),
    ("users", User),
]
# Preserved — application-wide, deliberately untouched.
PRESERVE = {"allowed_student_ids": AllowedStudentID, "groups": Group, "lessons": Lesson}

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def guard_local_dev() -> str:
    from urllib.parse import urlparse

    from app.config.settings import get_settings

    u = urlparse(get_settings().database_url)
    host = (u.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise SystemExit(
            f"REFUSING: configured database host {host!r} is not local. "
            "This script only targets a local development database."
        )
    if u.query and "sslmode" in u.query:
        raise SystemExit("REFUSING: SSL options suggest a remote database.")
    return f"{host}:{u.port or 5432}/{u.path.lstrip('/')}"


async def main() -> None:
    target = guard_local_dev()
    execute = "--execute" in sys.argv
    print(f"target database : {target}")
    print(f"mode            : {'EXECUTE' if execute else 'DRY RUN'}")

    async with get_session_factory()() as s:
        print("\n-- before --")
        for name, model in DELETE_ORDER + list(PRESERVE.items()):
            n = (await s.execute(select(func.count()).select_from(model))).scalar()
            tag = "preserve" if name in PRESERVE else "delete  "
            print(f"  [{tag}] {name:20} {n:6}")

        if not execute:
            print("\nDry run only. Re-run with --execute to apply.")
            return

        async with get_engine().begin() as conn:
            counts = {}
            for name, model in DELETE_ORDER:
                res = await conn.execute(
                    sa.delete(model.__table__))
                counts[name] = res.rowcount

        print("\n-- deleted --")
        for name, _ in DELETE_ORDER:
            print(f"  {name:20} {counts[name]:6}")

        print("\n-- after --")
        for name, model in DELETE_ORDER + list(PRESERVE.items()):
            n = (await s.execute(select(func.count()).select_from(model))).scalar()
            tag = "preserve" if name in PRESERVE else "delete  "
            print(f"  [{tag}] {name:20} {n:6}")

        left = (await s.execute(select(func.count()).select_from(User))).scalar()
        print(f"\nregistered users remaining: {left}")
        assert left == 0, "user table not empty after reset"
        print("OK")


if __name__ == "__main__":
    asyncio.run(main())