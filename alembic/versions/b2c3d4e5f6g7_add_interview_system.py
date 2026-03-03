"""add_interview_system

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-02-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6g7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── interview_tokens ─────────────────────────────────────────────────────
    op.create_table(
        'interview_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('token', sa.String(64), unique=True, nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_interview_tokens_token', 'interview_tokens', ['token'])
    op.create_index('ix_interview_tokens_job_id', 'interview_tokens', ['job_id'])

    # ── interview_sessions ───────────────────────────────────────────────────
    op.create_table(
        'interview_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('token_id', sa.Integer(), sa.ForeignKey('interview_tokens.id', ondelete='CASCADE'), nullable=False),
        sa.Column('candidate_id', sa.Integer(), sa.ForeignKey('candidates.id', ondelete='SET NULL'), nullable=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.Enum('pending', 'in_progress', 'completed', 'terminated', name='sessionstatus'), nullable=False, server_default='pending'),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('total_questions', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('current_question_index', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('violation_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('session_metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_interview_sessions_token_id', 'interview_sessions', ['token_id'])
    op.create_index('ix_interview_sessions_candidate_id', 'interview_sessions', ['candidate_id'])
    op.create_index('ix_interview_sessions_job_id', 'interview_sessions', ['job_id'])
    op.create_index('ix_interview_sessions_status', 'interview_sessions', ['status'])

    # ── interview_messages ───────────────────────────────────────────────────
    op.create_table(
        'interview_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('interview_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('question_index', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_interview_messages_session_id', 'interview_messages', ['session_id'])

    # ── violations ───────────────────────────────────────────────────────────
    op.create_table(
        'violations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('interview_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('violation_type', sa.Enum('tab_switch', 'fullscreen_exit', 'face_not_detected', 'multiple_faces', name='violationtype'), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_violations_session_id', 'violations', ['session_id'])

    # ── evaluations ──────────────────────────────────────────────────────────
    op.create_table(
        'evaluations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('interview_sessions.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('candidate_id', sa.Integer(), sa.ForeignKey('candidates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('technical_score', sa.Float(), nullable=True),
        sa.Column('communication_score', sa.Float(), nullable=True),
        sa.Column('problem_solving_score', sa.Float(), nullable=True),
        sa.Column('confidence_score', sa.Float(), nullable=True),
        sa.Column('overall_score', sa.Float(), nullable=True),
        sa.Column('recommendation', sa.Enum('Strong Hire', 'Hire', 'Maybe', 'No Hire', name='recommendation'), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('strengths', sa.Text(), nullable=True),
        sa.Column('weaknesses', sa.Text(), nullable=True),
        sa.Column('detailed_feedback', sa.Text(), nullable=True),
        sa.Column('meets_threshold', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_evaluations_candidate_id', 'evaluations', ['candidate_id'])

    # ── candidate_feedback ───────────────────────────────────────────────────
    op.create_table(
        'candidate_feedback',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('interview_sessions.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('rating', sa.Integer(), nullable=False),
        sa.Column('experience_comment', sa.Text(), nullable=True),
        sa.Column('technical_issues', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('would_recommend', sa.Boolean(), nullable=True, server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # ── meeting_slots ─────────────────────────────────────────────────────────
    op.create_table(
        'meeting_slots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('start_time', sa.DateTime(), nullable=False),
        sa.Column('end_time', sa.DateTime(), nullable=False),
        sa.Column('is_booked', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_meeting_slots_job_id', 'meeting_slots', ['job_id'])

    # ── meeting_schedules ─────────────────────────────────────────────────────
    op.create_table(
        'meeting_schedules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('interview_sessions.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('slot_id', sa.Integer(), sa.ForeignKey('meeting_slots.id', ondelete='CASCADE'), nullable=False),
        sa.Column('candidate_id', sa.Integer(), sa.ForeignKey('candidates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.Enum('scheduled', 'cancelled', name='meetingschedulestatus'), nullable=True, server_default='scheduled'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_meeting_schedules_candidate_id', 'meeting_schedules', ['candidate_id'])


def downgrade() -> None:
    op.drop_table('meeting_schedules')
    op.drop_table('meeting_slots')
    op.drop_table('candidate_feedback')
    op.drop_table('evaluations')
    op.drop_table('violations')
    op.drop_table('interview_messages')
    op.drop_table('interview_sessions')
    op.drop_table('interview_tokens')

    op.execute("DROP TYPE IF EXISTS meetingschedulestatus")
    op.execute("DROP TYPE IF EXISTS recommendation")
    op.execute("DROP TYPE IF EXISTS violationtype")
    op.execute("DROP TYPE IF EXISTS sessionstatus")
