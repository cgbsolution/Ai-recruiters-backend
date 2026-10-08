"""Talent Agent: shortlist sort order on talent_job_settings

Revision ID: s3t4u5v6w7x8
Revises: r2s3t4u5v6w7
Create Date: 2026-10-10 09:00:00.000000
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "s3t4u5v6w7x8"
down_revision = "r2s3t4u5v6w7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("talent_job_settings", sa.Column("shortlist", JSONB(), nullable=True))


def downgrade() -> None:
    from alembic import op

    if has_column("talent_job_settings", "shortlist"):
        op.drop_column("talent_job_settings", "shortlist")
