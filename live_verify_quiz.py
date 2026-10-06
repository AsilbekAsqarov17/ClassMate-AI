"""Live verification: real E-Class sync -> quiz status lifecycle (no secrets printed)."""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.database.database import get_session_factory
from app.database.models.assignment import Assignment
from app.database.models.eclass_account import EClassAccount
from app.database.models.user import User
from app.services.assignment_service import to_local
from app.services.eclass_session import client_for_account
from app.services.quiz_service import upcoming_quizzes
from app.services.sync_service import sync_user


async def main():
    async with get_session_factory()() as s:
        u = (await s.execute(select(User).where(User.username == "sync_test"))).scalar_one()
        acc = (await s.execute(select(EClassAccount).where(EClassAccount.user_id == u.id))).scalar_one()

        # 1) raw E-Class data
        client = await client_for_account(s, acc)
        quizzes = await client.get_quizzes()
        print(f"RAW E-CLASS QUIZZES: {len(quizzes)}")
        for q in quizzes:
            local = to_local(q.deadline, u.timezone)
            print(f"  [{q.external_id}] {q.title!r} close={local:%Y-%m-%d %H:%M if local else '-'} status={q.submission_status!r} score={q.score}/{q.max_score}")

        # 2) full sync through the production path
        outcome = await sync_user(s, acc)
        print(f"\nSYNC: courses={outcome.courses_ok} tt={outcome.timetable_ok} assign={outcome.assignments_ok} quiz={outcome.quizzes_ok} new_scores={len(outcome.new_scores)} err={outcome.error}")
        for ns in outcome.new_scores:
            print(f"  NEW SCORE pending notify: {ns.title} {ns.score}/{ns.max_score}")

        # 3) DB state after sync
        rows = (await s.execute(
            select(Assignment).where(Assignment.user_id == u.id).order_by(Assignment.kind, Assignment.deadline)
        )).scalars().all()
        print(f"\nDB WATCHLIST ({len(rows)} rows):")
        for r in rows:
            local = to_local(r.deadline, u.timezone)
            dl = f"{local:%Y-%m-%d %H:%M}" if local else "-"
            print(f"  {r.kind:8} [{r.external_id}] {r.title!r} deadline={dl} status={r.submission_status!r} score={r.score}/{r.max_score}")

        # 4) what /quizzes would show
        up = await upcoming_quizzes(s, u.id)
        print(f"\n/quizzes UPCOMING section: {[q.title for q in up]}")
        completed = [r for r in rows if r.kind == "quiz" and r.submission_status and not r.score and r not in up]
        print(f"/quizzes COMPLETED (awaiting score) section: {[q.title for q in completed]}")


asyncio.run(main())
