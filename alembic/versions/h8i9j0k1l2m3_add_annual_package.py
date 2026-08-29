"""Add annual_package column to resume_metadata

Revision ID: h8i9j0k1l2m3
Revises: f6g7h8i9j0k1
Create Date: 2026-03-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "h8i9j0k1l2m3"
down_revision = "f6g7h8i9j0k1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "resume_metadata",
        sa.Column("annual_package", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    if has_column("resume_metadata", "annual_package"):
        op.drop_column("resume_metadata", "annual_package")
