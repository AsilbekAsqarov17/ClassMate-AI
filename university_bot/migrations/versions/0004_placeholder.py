"""Placeholder for a revision that is missing from the repository.

Same situation as 0003: the live database is stamped at 0004, the revision
files were never committed, and the tables they created are already present.
See the 0003 stub for the full rationale.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: this revision's objects already exist in the live database."""


def downgrade() -> None:
    """No-op: nothing to undo."""