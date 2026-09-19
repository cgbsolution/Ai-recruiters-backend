"""Add interview_messages.feedback

Nira's reaction to the previous answer used to be saved in the same `content`
string as the next question, so the live interview spoke them as one run-on
line and the report's question cards showed the feedback mixed into the
question. The reaction now lives in its own nullable column; `content` holds
the question alone.

The same column is also ensured by startup.py and the raw DDL list in
app.main, so this migration is guarded against both having run first.

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2026-09-19 10:00:00.000000
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "l6m7n8o9p0q1"
down_revision = "k5l6m7n8o9p0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("interview_messages", sa.Column("feedback", sa.Text(), nullable=True))


def downgrade() -> None:
    from alembic import op

    if has_column("interview_messages", "feedback"):
        op.drop_column("interview_messages", "feedback")
