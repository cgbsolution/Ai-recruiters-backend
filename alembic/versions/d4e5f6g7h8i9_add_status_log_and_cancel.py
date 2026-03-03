"""add_status_log_and_cancel

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2026-03-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6g7h8i9'
down_revision: Union[str, None] = 'c3d4e5f6g7h8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widen interview_status column to accommodate longer status strings
    op.alter_column(
        'candidates',
        'interview_status',
        type_=sa.String(60),
        existing_type=sa.String(30),
        existing_nullable=True,
    )

    # Create interview_status_logs table for audit trail
    op.create_table(
        'interview_status_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('candidate_id', sa.Integer(), sa.ForeignKey('candidates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('previous_status', sa.String(60), nullable=True),
        sa.Column('new_status', sa.String(60), nullable=False),
        sa.Column('changed_by', sa.String(100), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_interview_status_logs_candidate_id', 'interview_status_logs', ['candidate_id'])


def downgrade() -> None:
    op.drop_index('ix_interview_status_logs_candidate_id', table_name='interview_status_logs')
    op.drop_table('interview_status_logs')
    op.alter_column(
        'candidates',
        'interview_status',
        type_=sa.String(30),
        existing_type=sa.String(60),
        existing_nullable=True,
    )
