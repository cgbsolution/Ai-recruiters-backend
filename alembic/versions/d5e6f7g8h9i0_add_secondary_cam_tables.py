"""Add secondary camera verification tables

Revision ID: d5e6f7g8h9i0
Revises: c3d4e5f6g7h8
Create Date: 2026-03-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "d5e6f7g8h9i0"
down_revision = "c3d4e5f6g7h8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = inspect(bind)
    existing = insp.get_table_names()

    if "secondary_cam_sessions" not in existing:
        op.create_table(
            "secondary_cam_sessions",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("pairing_code", sa.String(32), unique=True, nullable=False, index=True),
            sa.Column("status", sa.String(20), server_default="pending"),
            sa.Column("device_info", sa.Text(), nullable=True),
            sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
            sa.Column("positioning_snapshot", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("verified_at", sa.DateTime(), nullable=True),
        )

    if "secondary_cam_snapshots" not in existing:
        op.create_table(
            "secondary_cam_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("secondary_session_id", sa.Integer(), sa.ForeignKey("secondary_cam_sessions.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("file_path", sa.String(500), nullable=False),
            sa.Column("captured_at", sa.DateTime(), server_default=sa.func.now()),
        )


def downgrade():
    op.drop_table("secondary_cam_snapshots")
    op.drop_table("secondary_cam_sessions")
