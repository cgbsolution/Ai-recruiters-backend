"""add_candidate_extended_fields

Revision ID: a1b2c3d4e5f6
Revises: f9ffeeb88d8e
Create Date: 2026-02-25 00:00:00.000000

Every column is added only if the database does not already have it:
`startup.py` and `app.main`'s raw `ALTER TABLE ... IF NOT EXISTS` list create
most of these before Alembic ever runs, which used to abort the upgrade with
DuplicateColumn. See app/migration_helpers.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f9ffeeb88d8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns():
    """Fresh Column objects per call — a Column can only be attached to one Table."""
    return [
        # Resume fit score
        sa.Column('resume_fit_score', sa.String(length=20), nullable=True),
        sa.Column('fit_score_reasoning', sa.Text(), nullable=True),
        sa.Column('fit_score_details', sa.Text(), nullable=True),

        # Stage tracking
        sa.Column('candidate_stage', sa.String(length=50), nullable=True, server_default='Candidates'),

        # Interview scheduling
        sa.Column('interview_status', sa.String(length=30), nullable=True, server_default='New'),
        sa.Column('interview_date', sa.DateTime(), nullable=True),
        sa.Column('interview_timezone', sa.String(length=100), nullable=True),
        sa.Column('interview_link', sa.String(length=500), nullable=True),
        sa.Column('allow_candidate_reschedule', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('joining_flexibility', sa.String(length=30), nullable=True, server_default='anytime'),
        sa.Column('schedule_email_template', sa.Text(), nullable=True),

        # Post-interview scores
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('trust_score', sa.Float(), nullable=True),
        sa.Column('grammar_score', sa.Float(), nullable=True),
        sa.Column('confidence_score', sa.Float(), nullable=True),
        sa.Column('clarity_score', sa.Float(), nullable=True),
        sa.Column('pronunciation_score', sa.Float(), nullable=True),
        sa.Column('enthusiasm_score', sa.Float(), nullable=True),
        sa.Column('conciseness_score', sa.Float(), nullable=True),
        sa.Column('ai_cheating_score', sa.Float(), nullable=True),

        # Recording
        sa.Column('recording_link', sa.String(length=500), nullable=True),
        sa.Column('video_duration', sa.String(length=50), nullable=True),

        # Contact
        sa.Column('mail_contact_history', sa.Boolean(), nullable=True, server_default='false'),
    ]


def upgrade() -> None:
    for column in _columns():
        add_column_if_missing('candidates', column)


def downgrade() -> None:
    for column in reversed(_columns()):
        if has_column('candidates', column.name):
            op.drop_column('candidates', column.name)
