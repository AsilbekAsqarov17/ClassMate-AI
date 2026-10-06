from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.assignment import Assignment


async def scores(session: AsyncSession, user_id: int) -> list[Assignment]:
    result = await session.execute(
        select(Assignment)
        .where(Assignment.user_id == user_id, Assignment.score.is_not(None))
        .order_by(Assignment.course_name, Assignment.title)
    )
    return list(result.scalars())


def overall_average(items: list[Assignment]) -> float | None:
    values = []
    for a in items:
        try:
            score = float(str(a.score).split("/")[0])
            max_score = float(a.max_score) if a.max_score else None
            if max_score:
                values.append(score / max_score * 100)
        except (ValueError, TypeError):
            continue
    return sum(values) / len(values) if values else None
