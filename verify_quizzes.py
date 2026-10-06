import asyncio

from sqlalchemy import select

from app.database.database import get_session_factory
from app.database.models.assignment import Assignment
from app.database.models.user import User
from app.services.assignment_service import remaining_text, to_local
from app.services.grade_service import overall_average, scores
from app.services.quiz_service import format_quiz, upcoming_quizzes


async def main():
    async with get_session_factory()() as s:
        u = (await s.execute(select(User).where(User.username == "sync_test"))).scalar_one()
        quizzes = (await s.execute(
            select(Assignment).where(Assignment.user_id == u.id, Assignment.kind == "quiz").order_by(Assignment.deadline)
        )).scalars().all()
        print(f"QUIZZES STORED: {len(quizzes)}")
        for q in quizzes:
            local = to_local(q.deadline, u.timezone)
            dl = f"{local:%Y-%m-%d %H:%M}" if local else "-"
            print(f"  [{q.external_id}] {q.course_name} | {q.title} | close={dl} | status={q.submission_status} | score={q.score}/{q.max_score}")
        up = await upcoming_quizzes(s, u.id)
        print(f"\n/quizzes (upcoming): {len(up)}")
        for q in up:
            print("  ", q.course_name, q.title, to_local(q.deadline, u.timezone))
        # check a homework deadline rendering
        hw = (await s.execute(
            select(Assignment).where(Assignment.user_id == u.id, Assignment.kind != "quiz").where(Assignment.deadline.is_not(None)).order_by(Assignment.deadline)
        )).scalars().all()
        print(f"\nHOMEWORK deadlines (stored UTC -> Tashkent):")
        for h in hw[:6]:
            local = to_local(h.deadline, u.timezone)
            print(f"  {h.title}: stored={h.deadline}  -> shown={local:%Y-%m-%d %H:%M}  ({remaining_text(h.deadline)})")


asyncio.run(main())
