"""Create base tables (users, eclass_accounts) and apply initial password design.

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    # 1. Create users table with all required model columns if it doesn't exist
    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("username", sa.String(length=255), nullable=True),
            sa.Column("timezone", sa.String(length=64), server_default="Asia/Tashkent", nullable=False),
            sa.Column("group_id", sa.String(length=64), nullable=True),
            sa.Column("class_notifications", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("deadline_notifications", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("daily_timetable_notifications", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("reminder_minutes", sa.Integer(), server_default="15", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # 2. Create eclass_accounts table if it doesn't exist, otherwise drop legacy column safely
    if "eclass_accounts" not in tables:
        op.create_table(
            "eclass_accounts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("session_data", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        )
    else:
        columns = [col["name"] for col in inspector.get_columns("eclass_accounts")]
        if "encrypted_password" in columns:
            op.drop_column("eclass_accounts", "encrypted_password")

def downgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()
    
    if "eclass_accounts" in tables:
        columns = [col["name"] for col in inspector.get_columns("eclass_accounts")]
        if "encrypted_password" not in columns:
            op.add_column(
                "eclass_accounts",
                sa.Column("encrypted_password", sa.Text(), nullable=True),
            )