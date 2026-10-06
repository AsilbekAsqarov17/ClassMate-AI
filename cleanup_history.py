"""One-time cleanup: legacy graded rows are marked 'score notified' and the
new watchlist sync runs to garbage-collect them + expired unsubmitted rows."""
import asyncio

from sqlalchemy import select

from app.config.settings import get_settings
from app.database.database import get_session_factory
from app.database.models.assignment import Assignment
from app.database.models.course import Course  # noqa: F401
from app.database.models.eclass_account import EClassAccount
from app.database.models.group import Group  # noqa: F401
from app.database.models.lesson import Lesson  # noqa: F401
from app.database.models.notification import NotificationRecord
from app.database.models.user import User  # noqa: F401
from app.services.eclass_session import client_for_account
from app.services.sync_service import sync_user


async def main():
    from datetime import datetime, timezone

    async with get_session_factory()() as s:
        # mark legacy graded rows as notified (historical, so no Telegram spam)
        rows = (await s.execute(select(Assignment).where(Assignment.score.is_not(None)))).scalars().all()
        for r in rows:
            s.add(NotificationRecord(user_id=r.user_id, kind="score", ref_id=str(r.external_id), sent_at=datetime.now(timezone.utc)))
        await s.commit()
        print("marked notified:", len(rows))

        accounts = (await s.execute(select(EClassAccount).where(EClassAccount.is_active.is_(True)))).scalars().all()
        for acc in accounts:
            out = await sync_user(s, acc)
            print(f"user {acc.user_id}: tt_ok={out.timetable_ok} new_scores={len(out.new_scores)} err={out.error}")
        await s.commit()


asyncio.run(main())
