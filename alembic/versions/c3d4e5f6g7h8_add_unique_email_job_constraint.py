"""add_unique_email_job_constraint

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-03-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6g7h8'
down_revision: Union[str, None] = 'b2c3d4e5f6g7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Clean up duplicate candidates (keep lowest ID per email+job_id)
    # First, reassign interview_sessions from duplicate candidate IDs to the kept ones
    conn = op.get_bind()

    # Find duplicates: for each (email, job_id) pair with multiple candidates,
    # keep the one with the lowest id
    dupes = conn.execute(sa.text("""
        SELECT c.id AS dup_id, keeper.id AS keep_id
        FROM candidates c
        INNER JOIN (
            SELECT MIN(id) AS id, email, job_id
            FROM candidates
            WHERE job_id IS NOT NULL
            GROUP BY email, job_id
            HAVING COUNT(*) > 1
        ) keeper ON c.email = keeper.email AND c.job_id = keeper.job_id AND c.id != keeper.id
    """)).fetchall()

    for dup_id, keep_id in dupes:
        # Reassign interview_sessions
        conn.execute(sa.text(
            "UPDATE interview_sessions SET candidate_id = :keep_id WHERE candidate_id = :dup_id"
        ), {"keep_id": keep_id, "dup_id": dup_id})

        # Reassign evaluations
        conn.execute(sa.text(
            "UPDATE evaluations SET candidate_id = :keep_id WHERE candidate_id = :dup_id"
        ), {"keep_id": keep_id, "dup_id": dup_id})

        # Reassign meeting_schedules
        conn.execute(sa.text(
            "UPDATE meeting_schedules SET candidate_id = :keep_id WHERE candidate_id = :dup_id"
        ), {"keep_id": keep_id, "dup_id": dup_id})

        # Delete the duplicate candidate
        conn.execute(sa.text(
            "DELETE FROM candidates WHERE id = :dup_id"
        ), {"dup_id": dup_id})

    # Step 2: Add unique constraint
    op.create_unique_constraint('uq_candidate_email_job', 'candidates', ['email', 'job_id'])


def downgrade() -> None:
    op.drop_constraint('uq_candidate_email_job', 'candidates', type_='unique')
