"""AskMira connection key: the one-row askmira_connection table

Revision ID: b7l2n3s4b5m6
Revises: a6k1m2r3a4l5
Create Date: 2026-10-21 12:00:00.000000

Lets the platform super admin connect AskMira from the app with a connection
key instead of setting ASKMIRA_SHARED_SECRET. Additive only — safe on a live
database.
"""
import sqlalchemy as sa

from app.migration_helpers import create_table_if_missing, has_table

# revision identifiers
revision = "b7l2n3s4b5m6"
down_revision = "a6k1m2r3a4l5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "askmira_connection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active_nonce", sa.String(64), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=True),
        sa.Column("pending_nonce", sa.String(64), nullable=True),
        sa.Column("pending_expires_at", sa.DateTime(), nullable=True),
        sa.Column("generated_by_user_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    from alembic import op

    if has_table("askmira_connection"):
        op.drop_table("askmira_connection")
