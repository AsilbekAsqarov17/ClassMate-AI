"""Placeholder for a revision that is missing from the repository.

The deployed database is stamped at revision 0004, but the repository only ever
contained 0001 and 0002. Whatever 0003 and 0004 originally did, their effect is
already present in the live database (`allowed_student_ids`, `attendance` and
`attendance_records` tables exist there and are mapped by
app.database.models.allowed_student / attendance).

These stubs exist purely so the revision chain is continuous and Alembic can
resolve `down_revision` for later migrations. They intentionally do nothing:
re-creating the tables would fail against the live database, and skipping them
would silently desynchronise a fresh checkout from production.

To reconstruct the true history, recover the original 0003/0004 files and
replace these stubs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: this revision's objects already exist in the live database."""


def downgrade() -> None:
    """No-op: nothing to undo."""