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

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("eclass_accounts", "encrypted_password")


def downgrade() -> None:
    op.add_column(
        "eclass_accounts",
        sa.Column("encrypted_password", sa.Text(), nullable=True),
    )
