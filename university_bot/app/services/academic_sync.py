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


def score_ref(external_id: str, score: str | None) -> str:
    """Deduplication key for a grade notification.

    Includes the score so a corrected grade is a genuinely new event and gets
    its own message, while an unchanged score keeps hitting the same key and
    is never re-sent. max_score is deliberately excluded: E-Class sometimes
    publishes the mark first and the maximum later, and that must not read as
    a grade change.
    """
    return f"{external_id}#{score}" if score is not None else str(external_id)


async def score_notification_sent(session: AsyncSession, user_id: int, ref: str) -> bool:
    """True when a grade notification for this exact key was already delivered.

    Two keys count as delivered for this ref:
      * the exact key "external_id#score" — a precise per-grade match.
      * the legacy bare "external_id" written before score_ref() existed. Its
        score is not recoverable, so it is honoured as "this item was
        announced" rather than risk re-sending a score the student may
        already hold. Legacy items therefore do not re-announce corrections;
        items first recorded after this change get per-grade precision.

    A DIFFERENT grade ("hw#7" after "hw#5") is not matched here — that is the
    corrected-grade case and must notify. Use
    score_notification_sent_for_item() for "was this item ever announced".
    """
    external_id = ref.split("#", 1)[0]
    r = await session.execute(
        select(NotificationRecord.id).where(
            NotificationRecord.user_id == user_id,
            NotificationRecord.kind == "score",
            NotificationRecord.ref_id.in_([ref, external_id]),
        ).limit(1)
    )
    return r.first() is not None


async def score_notification_sent_for_item(session: AsyncSession, user_id: int, external_id: str) -> bool:
    """True when ANY grade notification exists for this item.

    Used by the cleanup rule so a delivered row is still removed even after
    its grade has been corrected and re-notified.
    """
    # An item can have several notifications (original grade + corrections),
    # so this is an existence check, not a single-row fetch.
    r = await session.execute(
        select(NotificationRecord.id).where(
            NotificationRecord.user_id == user_id,
            NotificationRecord.kind == "score",
            NotificationRecord.ref_id.like(f"{external_id}#%"),
        ).limit(1)
    )
    if r.first() is not None:
        return True
    # legacy rows written before score_ref() carry the bare external_id
    r = await session.execute(
        select(NotificationRecord.id).where(
            NotificationRecord.user_id == user_id,
            NotificationRecord.kind == "score",
            NotificationRecord.ref_id == str(external_id),
        ).limit(1)
    )
    return r.first() is not None


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

    # Graded items: deliver once per distinct score.
    #
    # Policy:
    #   * first discovery of an already-graded item  -> notify (it has a grade
    #     the student has not been told about).
    #   * grade changes to a new value              -> notify again, because the
    #     earlier message told them a different mark.
    #   * same grade again                          -> never notify (keyed by
    #     external_id#score).
    #   * previous send failed (no record)          -> stays eligible and is
    #     retried on the next sync.
    # Rows are deleted only once a notification for the current grade exists,
    # so nothing is cleaned up ahead of its message.
    for row in list(existing.values()):
        if row.score is not None:
            ref = score_ref(row.external_id, row.score)
            if await score_notification_sent(session, user_id, ref):
                await session.delete(row)   # this grade was already delivered
            elif not any(x.external_id == row.external_id for x in new_scores):
                # newly published, newly corrected, or a previous attempt failed
                new_scores.append(row)
            continue
        if not _keep_row(row, now):
            await session.delete(row)

    # first pass over fetched items is unnecessary for 'new' detection:
    # _keep_row/notification covers everything uniformly.
    return new_scores
