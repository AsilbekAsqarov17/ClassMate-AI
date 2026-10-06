"""Migrate: users.group_name -> groups table; lessons.user_id -> lessons.group_id."""
import asyncio

from sqlalchemy import text

from app.database.database import get_engine, create_tables


async def main():
    # create new 'groups' table first
    await create_tables()
    eng = get_engine()
    async with eng.begin() as c:
        await c.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS group_id integer REFERENCES groups(id)"))
        await c.execute(text("ALTER TABLE lessons ADD COLUMN IF NOT EXISTS group_id integer REFERENCES groups(id)"))

        # create Group rows keyed by normalized group name
        rows = (await c.execute(text("SELECT DISTINCT group_name FROM users WHERE group_name IS NOT NULL"))).all()
        for (name,) in rows:
            canonical = "-".join(part for part in __import__("re").findall(r"[A-Za-z]+|\d+", name)).upper()
            # normalize digits: strip leading zeros per token
            import re

            toks = re.findall(r"[a-zA-Z]+|\d+", name)
            norm = "-".join(t.upper() if t.isalpha() else str(int(t)) for t in toks)
            await c.execute(
                text("INSERT INTO groups (name) VALUES (:n) ON CONFLICT (name) DO NOTHING"),
                {"n": norm},
            )
        await c.execute(text("""
            UPDATE users SET group_id = g.id FROM groups g
            WHERE g.name = (SELECT 'x') AND false
        """)) if False else None
        # set users.group_id
        rows = (await c.execute(text("SELECT id, group_name FROM users WHERE group_name IS NOT NULL"))).all()
        import re

        for uid, name in rows:
            toks = re.findall(r"[a-zA-Z]+|\d+", name)
            norm = "-".join(t.upper() if t.isalpha() else str(int(t)) for t in toks)
            await c.execute(
                text("UPDATE users SET group_id = (SELECT id FROM groups WHERE name = :n) WHERE id = :u"),
                {"n": norm, "u": uid},
            )
        # lessons: map via user
        await c.execute(text("UPDATE lessons SET group_id = u.group_id FROM users u WHERE lessons.user_id = u.id"))
        # dedupe shared-group copies: keep lowest id per (group_id, external_id)
        await c.execute(text("""
            DELETE FROM lessons a USING lessons b
            WHERE a.id > b.id AND a.group_id = b.group_id AND a.external_id = b.external_id
        """))
        await c.execute(text("ALTER TABLE lessons ALTER COLUMN group_id SET NOT NULL"))
        await c.execute(text("DROP INDEX IF EXISTS ix_lessons_user_date"))
        await c.execute(text("ALTER TABLE lessons DROP COLUMN user_id"))
        await c.execute(text("ALTER TABLE users DROP COLUMN group_name"))
    print("migration done")


asyncio.run(main())
