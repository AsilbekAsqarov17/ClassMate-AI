"""Group registry: one shared timetable per academic group."""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.group import Group


def group_key(name: str) -> tuple:
    toks = re.findall(r"[a-z]+|\d+", (name or "").lower())
    return tuple(int(t) if t.isdigit() else t for t in toks)


async def get_or_create_group(session: AsyncSession, name: str) -> Group:
    """Match 'ICE-24-01' to existing group 'ICE24-1' (generic normalization)."""
    wanted = group_key(name)
    result = await session.execute(select(Group))
    for g in result.scalars():
        if group_key(g.name) == wanted:
            return g
    g = Group(name=name.strip())
    session.add(g)
    await session.flush()
    return g
