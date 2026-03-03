"""add_candidate_extended_fields

Revision ID: a1b2c3d4e5f6
Revises: f9ffeeb88d8e
Create Date: 2026-02-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f9ffeeb88d8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Resume fit score
    op.add_column('candidates', sa.Column('resume_fit_score', sa.String(length=20), nullable=True))
    op.add_column('candidates', sa.Column('fit_score_reasoning', sa.Text(), nullable=True))
    op.add_column('candidates', sa.Column('fit_score_details', sa.Text(), nullable=True))

    # Stage tracking
    op.add_column('candidates', sa.Column('candidate_stage', sa.String(length=50), nullable=True, server_default='Candidates'))

    # Interview scheduling
    op.add_column('candidates', sa.Column('interview_status', sa.String(length=30), nullable=True, server_default='New'))
    op.add_column('candidates', sa.Column('interview_date', sa.DateTime(), nullable=True))
    op.add_column('candidates', sa.Column('interview_timezone', sa.String(length=100), nullable=True))
    op.add_column('candidates', sa.Column('interview_link', sa.String(length=500), nullable=True))
    op.add_column('candidates', sa.Column('allow_candidate_reschedule', sa.Boolean(), nullable=True, server_default='false'))
    op.add_column('candidates', sa.Column('joining_flexibility', sa.String(length=30), nullable=True, server_default='anytime'))
    op.add_column('candidates', sa.Column('schedule_email_template', sa.Text(), nullable=True))

    # Post-interview scores
    op.add_column('candidates', sa.Column('score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('trust_score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('grammar_score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('confidence_score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('clarity_score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('pronunciation_score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('enthusiasm_score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('conciseness_score', sa.Float(), nullable=True))
    op.add_column('candidates', sa.Column('ai_cheating_score', sa.Float(), nullable=True))

    # Recording
    op.add_column('candidates', sa.Column('recording_link', sa.String(length=500), nullable=True))
    op.add_column('candidates', sa.Column('video_duration', sa.String(length=50), nullable=True))

    # Contact
    op.add_column('candidates', sa.Column('mail_contact_history', sa.Boolean(), nullable=True, server_default='false'))


def downgrade() -> None:
    op.drop_column('candidates', 'mail_contact_history')
    op.drop_column('candidates', 'video_duration')
    op.drop_column('candidates', 'recording_link')
    op.drop_column('candidates', 'ai_cheating_score')
    op.drop_column('candidates', 'conciseness_score')
    op.drop_column('candidates', 'enthusiasm_score')
    op.drop_column('candidates', 'pronunciation_score')
    op.drop_column('candidates', 'clarity_score')
    op.drop_column('candidates', 'confidence_score')
    op.drop_column('candidates', 'grammar_score')
    op.drop_column('candidates', 'trust_score')
    op.drop_column('candidates', 'score')
    op.drop_column('candidates', 'schedule_email_template')
    op.drop_column('candidates', 'joining_flexibility')
    op.drop_column('candidates', 'allow_candidate_reschedule')
    op.drop_column('candidates', 'interview_link')
    op.drop_column('candidates', 'interview_timezone')
    op.drop_column('candidates', 'interview_date')
    op.drop_column('candidates', 'interview_status')
    op.drop_column('candidates', 'candidate_stage')
    op.drop_column('candidates', 'fit_score_details')
    op.drop_column('candidates', 'fit_score_reasoning')
    op.drop_column('candidates', 'resume_fit_score')
