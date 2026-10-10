"""Create allowed_student_ids and seed the initial whitelist.

Student IDs are stored normalized (lowercase), matching
normalize_student_id(). Add more rows later with plain SQL, e.g.:

    INSERT INTO allowed_student_ids (student_id) VALUES ('u2410060');

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

INITIAL_ALLOWED_STUDENT_IDS = [
    "u2410026", "u2410027", "u2410028", "u2410029", "u2410030",
    "u2410031", "u2410032", "u2410033", "u2410034", "u2410035",
    "u2410036", "u2410037", "u2410038", "u2410039", "u2410040",
    "u2410041", "u2410042", "u2410043", "u2410044", "u2410045",
    "u2410046", "u2410047", "u2410048", "u2410049", "u2410050",
    "u2410015", "u2410101", "u2410189", "u2410253", "u2410255",
]


def upgrade() -> None:
    op.create_table(
        "allowed_student_ids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_id"),
    )
    op.create_index(
        op.f("ix_allowed_student_ids_student_id"),
        "allowed_student_ids",
        ["student_id"],
        unique=True,
    )
    table = sa.table(
        "allowed_student_ids",
        sa.column("student_id", sa.String()),
    )
    op.bulk_insert(
        table,
        [{"student_id": sid} for sid in INITIAL_ALLOWED_STUDENT_IDS],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_allowed_student_ids_student_id"), table_name="allowed_student_ids")
    op.drop_table("allowed_student_ids")
