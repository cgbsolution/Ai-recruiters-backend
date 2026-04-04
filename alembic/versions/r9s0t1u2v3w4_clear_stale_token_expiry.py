"""Clear stale expires_at on interview tokens with scheduled candidates.

When a recruiter selects "No expiry" during scheduling, the old code did not
clear the existing expires_at value, causing interview links to show as expired
even though they should be permanently active. This migration:

1. Sets expires_at = NULL for all tokens where candidates still have
   "Scheduled" status (these links must remain accessible).
2. Sets expires_at = NULL for all tokens where expires_at is already in the
   past but the token is still marked is_active = True (stale expiry).

Revision ID: r9s0t1u2v3w4
Revises: q7r8s9t0u1v2
Create Date: 2026-04-04
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime

# revision identifiers
revision = "r9s0t1u2v3w4"
down_revision = "q7r8s9t0u1v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Clear expires_at for tokens that have ANY candidate with "Scheduled" status.
    # These tokens must remain accessible until the candidate takes the interview.
    conn.execute(
        sa.text("""
            UPDATE interview_tokens
            SET expires_at = NULL
            WHERE is_active = TRUE
              AND expires_at IS NOT NULL
              AND job_id IN (
                  SELECT DISTINCT job_id FROM candidates
                  WHERE interview_status = 'Scheduled'
              )
        """)
    )

    # 2. Clear expires_at for any active token where expiry is already in the past.
    # These are stale values that block candidates from accessing their interviews.
    conn.execute(
        sa.text("""
            UPDATE interview_tokens
            SET expires_at = NULL
            WHERE is_active = TRUE
              AND expires_at IS NOT NULL
              AND expires_at < :now
        """),
        {"now": datetime.utcnow()},
    )


def downgrade() -> None:
    # No reversal needed — we only cleared stale data.
    # Setting arbitrary expires_at values would be worse than leaving them NULL.
    pass
