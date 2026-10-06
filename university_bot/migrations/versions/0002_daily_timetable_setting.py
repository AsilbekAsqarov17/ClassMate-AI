"""Add users.daily_timetable_notifications (daily 1-hour timetable summary toggle).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "daily_timetable_notifications",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "daily_timetable_notifications")
