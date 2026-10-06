import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database.database import get_session_factory
from app.database.models.assignment import Assignment
from app.database.models.notification import NotificationRecord
from app.database.models.user import User
from app.services.notification_service import _aware, due_deadline_reminders


async def main():
    async with get_session_factory()() as s:
        u = (await s.execute(select(User).where(User.username == "sync_test"))).scalar_one()
        now_real = datetime.now(timezone.utc)
        a = (await s.execute(
            select(Assignment).where(Assignment.user_id == u.id)
            .where(Assignment.deadline.is_not(None), Assignment.deadline > now_real)
            .order_by(Assignment.deadline)
        )).scalars().first()
        print("using:", a.title, "deadline:", a.deadline, "kind:", a.kind, "ref_id:", a.id)
        deadline = _aware(a.deadline)

        # clear reminder history for this ref to re-test cleanly
        from sqlalchemy import delete

        await s.execute(delete(NotificationRecord).where(NotificationRecord.ref_id == str(a.id)))
        await s.commit()

        cases = [
            ("deadline - 48h       (expect 48h)", deadline - timedelta(hours=48)),
            ("deadline - 47h54m    (expect none, missed 48h)", deadline - timedelta(hours=47, minutes=54)),
            ("deadline - 30h       (expect none)", deadline - timedelta(hours=30)),
            ("deadline - 1h        (expect 1h)", deadline - timedelta(hours=1)),
            ("deadline - 1h again  (expect none, dedup)", deadline - timedelta(hours=1)),
            ("deadline - 30m       (expect none)", deadline - timedelta(minutes=30)),
        ]
        for label, now in cases:
            reminders = await due_deadline_reminders(s, u, now)
            mine = [k for _a, k in reminders if _a.id == a.id]
            print(f"{label} -> {mine}")

        rows = (await s.execute(
            select(NotificationRecord).where(NotificationRecord.user_id == u.id, NotificationRecord.ref_id == str(a.id))
        )).scalars().all()
        print("\nhistory for this assignment:", [(r.kind, r.sent_at) for r in rows])


asyncio.run(main())
