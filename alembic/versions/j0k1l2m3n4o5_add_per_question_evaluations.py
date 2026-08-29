"""Add per_question_evaluations and skills_ratings columns to evaluations

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-03-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "j0k1l2m3n4o5"
down_revision = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("evaluations", sa.Column("per_question_evaluations", sa.Text(), nullable=True))
    add_column_if_missing("evaluations", sa.Column("skills_ratings", sa.Text(), nullable=True))


def downgrade() -> None:
    for column in ("skills_ratings", "per_question_evaluations"):
        if has_column("evaluations", column):
            op.drop_column("evaluations", column)
