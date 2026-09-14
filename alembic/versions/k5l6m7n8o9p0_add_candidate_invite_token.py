"""Add candidates.invite_token for personal interview links

Interview links were per job, so anyone holding a job's link and a candidate's
email could open that candidate's interview session. Each scheduled candidate
now gets a secret invite token (app.invites). Nullable: candidates scheduled
before this change keep their emailed ?candidate=<email> links until they are
rescheduled.

The same column/index are also ensured by startup.py and the raw DDL list in
app.main, so this migration is guarded against both having run first.

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-09-12 10:00:00.000000
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, create_index_if_missing, has_column, has_index

# revision identifiers
revision = "k5l6m7n8o9p0"
down_revision = "j4k5l6m7n8o9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("candidates", sa.Column("invite_token", sa.String(64), nullable=True))
    create_index_if_missing("ix_candidates_invite_token", "candidates", ["invite_token"], unique=True)


def downgrade() -> None:
    from alembic import op

    if has_index("candidates", "ix_candidates_invite_token"):
        op.drop_index("ix_candidates_invite_token", table_name="candidates")
    if has_column("candidates", "invite_token"):
        op.drop_column("candidates", "invite_token")
