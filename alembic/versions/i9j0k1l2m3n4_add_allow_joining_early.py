"""Add allow_joining_early column to candidates

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-03-18 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "i9j0k1l2m3n4"
down_revision = "h8i9j0k1l2m3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("candidates", sa.Column("allow_joining_early", sa.Boolean(), nullable=True, server_default="false"))


def downgrade() -> None:
    if has_column("candidates", "allow_joining_early"):
        op.drop_column("candidates", "allow_joining_early")
