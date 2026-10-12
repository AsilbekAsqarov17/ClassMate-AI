"""Add attendance_report_deliveries (weekly-report idempotency ledger).

The live database is stamped at revision 0004, but only 0001 and 0002 are
committed to the repository. Revisions 0003 and 0004 created the
allowed_student_ids, attendance and attendance_records tables that already
exist in the deployed database; their original files are missing, so this
migration advances from 0004 and creates only the new table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attendance_report_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Guarantees one report per student per reporting week even with
        # concurrent workers or a retried job.
        sa.UniqueConstraint("user_id", "week_start", name="uq_attendance_report_week"),
    )
    op.create_index(
        "ix_attendance_report_deliveries_user_id", "attendance_report_deliveries", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_attendance_report_deliveries_user_id", table_name="attendance_report_deliveries")
    op.drop_table("attendance_report_deliveries")