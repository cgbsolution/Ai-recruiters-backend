"""add observer_tokens table

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-05-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade():
    insp = inspect(op.get_bind())
    if "observer_tokens" in insp.get_table_names():
        return
    op.create_table(
        "observer_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("interview_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("room_name", sa.String(120), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_email", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_observer_tokens_token", "observer_tokens", ["token"], unique=True)
    op.create_index("ix_observer_tokens_candidate_id", "observer_tokens", ["candidate_id"])
    op.create_index("ix_observer_tokens_job_id", "observer_tokens", ["job_id"])
    op.create_index("ix_observer_tokens_session_id", "observer_tokens", ["session_id"])
    op.create_index("ix_observer_tokens_room_name", "observer_tokens", ["room_name"])
    op.create_index("ix_observer_tokens_created_by_user_id", "observer_tokens", ["created_by_user_id"])


def downgrade():
    op.drop_index("ix_observer_tokens_created_by_user_id", table_name="observer_tokens")
    op.drop_index("ix_observer_tokens_room_name", table_name="observer_tokens")
    op.drop_index("ix_observer_tokens_session_id", table_name="observer_tokens")
    op.drop_index("ix_observer_tokens_job_id", table_name="observer_tokens")
    op.drop_index("ix_observer_tokens_candidate_id", table_name="observer_tokens")
    op.drop_index("ix_observer_tokens_token", table_name="observer_tokens")
    op.drop_table("observer_tokens")
