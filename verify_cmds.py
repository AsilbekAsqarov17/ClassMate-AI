import asyncio

from sqlalchemy import select

from app.database.database import get_session_factory
from app.database.models.user import User
from app.services.assignment_service import format_assignment, overdue, upcoming
from app.services.grade_service import overall_average, scores
from app.services.quiz_service import format_quiz, upcoming_quizzes


async def main():
    async with get_session_factory()() as s:
        u = (await s.execute(select(User).where(User.username == "sync_test"))).scalar_one()
        items = await upcoming(s, u.id, kind=None)
        items = [a for a in items if a.kind != "quiz"]
        from app.services.assignment_service import to_local

        print(f"/assignments -> {len(items)} rows")
        for a in items:
            dlp = to_local(a.deadline, u.timezone)
            dl_str = f"{dlp:%Y-%m-%d %H:%M}" if dlp else "-"
            print(f"  {a.course_name} | {a.title} | due={dl_str} | status={a.submission_status} | score={a.score}/{a.max_score}")
        ad = await upcoming(s, u.id)
        print(f"/deadlines -> {len(ad)} rows")
        for a in ad[:12]:
            dlp = to_local(a.deadline, u.timezone)
            dl_str = f"{dlp:%Y-%m-%d %H:%M}" if dlp else "-"
            print(f"  {a.course_name} | {a.title} | {dl_str}")
        q = await upcoming_quizzes(s, u.id)
        print(f"/quizzes -> {len(q)} rows")
        sc = await scores(s, u.id)
        print(f"/scores -> {len(sc)} rows")
        for a in sc:
            print(f"  {a.course_name} | {a.title} | {a.score}/{a.max_score}")
        print("average:", overall_average(sc))
        od = await overdue(s, u.id)
        print(f"overdue (deadline passed): {len(od)} rows")


asyncio.run(main())
