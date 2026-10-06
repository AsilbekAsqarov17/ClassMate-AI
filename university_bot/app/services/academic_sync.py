"""Academic watchlist sync: upsert by external_id + newly-graded detection + cleanup.

Only upcoming items, submitted-awaiting-grade items, and graded items whose
score notification has not been delivered yet are kept. Everything else
(expired + not submitted, submitted long ago with score already delivered,
actionable no-deadline items that finished) is removed.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.assignment import Assignment
from app.database.models.notification import NotificationRecord
from app.eclass.models import Assignment as EAssignment

NON_SUBMISSIONS = {"", "-", "no submission", "not submitted"}


def is_submitted(status: str | None) -> bool:
    if status is None:
        return False
    return status.strip().lower() not in NON_SUBMISSIONS


async def score_notification_sent(session: AsyncSession, user_id: int, external_id: str) -> bool:
    r = await session.execute(
        select(NotificationRecord).where(
            NotificationRecord.user_id == user_id,
            NotificationRecord.kind == "score",
            NotificationRecord.ref_id == str(external_id),
        )
    )
    return r.scalar_one_or_none() is not None


def _keep_row(row: Assignment, now: datetime) -> bool:
    submitted = is_submitted(row.submission_status)
    if row.score:
        # graded item: only kept until the score notification goes out
        return True
    if row.deadline is None:
        # no deadline: keep only while still actionable (nothing submitted yet)
        return not submitted
    if row.deadline > now:
        return True  # upcoming
    if submitted:
        return True  # submitted + awaiting grade
    return False  # expired and not submitted


async def sync_academic_records(
    session: AsyncSession,
    user_id: int,
    items: list[EAssignment],
) -> list[Assignment]:
    """Returns rows whose score was newly seen and whose score notification has
    not been sent yet (caller must notify + delete them)."""

    now = datetime.now(timezone.utc)
    rows = (await session.execute(select(Assignment).where(Assignment.user_id == user_id))).scalars().all()
    existing = {r.external_id: r for r in rows}
    new_scores: list[Assignment] = []

    for it in items:
        row = existing.get(it.external_id)
        old_score = row.score if row else None
        if row is None:
            row = Assignment(user_id=user_id, external_id=it.external_id, kind=it.kind, title=it.title, course_name=it.course_name)
            session.add(row)
            existing[it.external_id] = row
        row.course_name = it.course_name
        row.title = it.title
        row.kind = it.kind
        row.deadline = it.deadline
        row.open_time = it.open_time
        row.submission_status = it.submission_status
        row.score = it.score
        row.max_score = it.max_score
        row.url = it.submission_url

    await session.flush()

    # graded & not-yet-notified items are delivered (and retried on failure);
    # a 'score' notification record unique per user+external_id prevents dup spam.
    for row in list(existing.values()):
        if row.score:
            if await score_notification_sent(session, user_id, row.external_id):
                await session.delete(row)  # grade already delivered
            elif row.external_id not in {x.external_id for x in new_scores}:
                # newly published OR notification previously failed -> deliver now
                new_scores.append(row)
            continue
        if not _keep_row(row, now):
            await session.delete(row)

    # first pass over fetched items is unnecessary for 'new' detection:
    # _keep_row/notification covers everything uniformly.
    return new_scores
