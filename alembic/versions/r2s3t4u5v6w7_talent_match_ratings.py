"""Talent Agent: recruiter ratings on job matches (precision@10 report)

Revision ID: r2s3t4u5v6w7
Revises: q1r2s3t4u5v6
Create Date: 2026-10-09 18:00:00.000000
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "r2s3t4u5v6w7"
down_revision = "q1r2s3t4u5v6"
branch_labels = None
depends_on = None

COLUMNS = (
    sa.Column("recruiter_rating", sa.String(16), nullable=True),
    sa.Column("rated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.Column("rated_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    for column in COLUMNS:
        add_column_if_missing("job_matches", column)


def downgrade() -> None:
    from alembic import op

    for column in COLUMNS:
        if has_column("job_matches", column.name):
            op.drop_column("job_matches", column.name)
