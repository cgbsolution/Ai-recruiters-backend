"""Add per_question_evaluations and skills_ratings columns to evaluations

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-03-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "j0k1l2m3n4o5"
down_revision = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("per_question_evaluations", sa.Text(), nullable=True))
    op.add_column("evaluations", sa.Column("skills_ratings", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluations", "skills_ratings")
    op.drop_column("evaluations", "per_question_evaluations")
