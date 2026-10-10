"""Create attendance table (persisted per-user/course E-Class attendance).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attendance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("course_external_id", sa.String(length=64), nullable=False),
        sa.Column("course_name", sa.String(length=512), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("absences", sa.Integer(), nullable=True),
        sa.Column("present", sa.Integer(), nullable=True),
        sa.Column("late", sa.Integer(), nullable=True),
        sa.Column("excused", sa.Integer(), nullable=True),
        sa.Column("records_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "course_external_id", name="uq_attendance_user_course"),
    )
    op.create_index(op.f("ix_attendance_user_id"), "attendance", ["user_id"])
    op.create_index(op.f("ix_attendance_course_external_id"), "attendance", ["course_external_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_attendance_course_external_id"), table_name="attendance")
    op.drop_index(op.f("ix_attendance_user_id"), table_name="attendance")
    op.drop_table("attendance")
