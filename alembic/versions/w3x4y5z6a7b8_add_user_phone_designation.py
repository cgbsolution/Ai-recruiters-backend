"""Add phone and designation to users table

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-03-29 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

revision = "w3x4y5z6a7b8"
down_revision = "v2w3x4y5z6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("users", sa.Column("phone", sa.String(50), nullable=True))
    add_column_if_missing("users", sa.Column("designation", sa.String(150), nullable=True))


def downgrade() -> None:
    for column in ("designation", "phone"):
        if has_column("users", column):
            op.drop_column("users", column)
