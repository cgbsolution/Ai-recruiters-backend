"""Mark the users the AskMira link itself created

Revision ID: e0p5q6r7s8t9
Revises: d9n4p5u6d7o8
Create Date: 2026-11-10 12:00:00.000000

The AskMira people sync switched off every user carrying askmira_user_id who
was missing from a roster. That id is also stamped on accounts made here the
moment they open the tool from AskMira, so admins made here were being
switched off. Now only users the link created (askmira_managed) can be.

Existing rows start as false: nobody already here is switched off by a sync
until AskMira creates them anew. Additive only — safe on a live database.
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "e0p5q6r7s8t9"
down_revision = "d9n4p5u6d7o8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "users",
        sa.Column("askmira_managed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    from alembic import op

    if has_column("users", "askmira_managed"):
        op.drop_column("users", "askmira_managed")
