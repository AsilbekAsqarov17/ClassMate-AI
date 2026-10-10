"""One-time password design: drop eclass_accounts.encrypted_password.

The E-Class password is used once at login and never persisted. Only the
authenticated session (encrypted cookies in session_data) is stored.
Existing rows keep their session_data; the password column is dropped
without being read, copied, or logged.

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

    if "eclass_accounts" not in tables:
        # Fresh database: create the table without encrypted_password
        op.create_table(
            "eclass_accounts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("session_data", sa.Text(), nullable=True),
        )
    else:
        # Existing database: drop the legacy column if it exists
        columns = [col["name"] for col in inspector.get_columns("eclass_accounts")]
        if "encrypted_password" in columns:
            op.drop_column("eclass_accounts", "encrypted_password")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns = [col["name"] for col in inspector.get_columns("eclass_accounts")]
    if "encrypted_password" not in columns:
        op.add_column(
            "eclass_accounts",
            sa.Column("encrypted_password", sa.Text(), nullable=True),
        )