"""Add interview_screenshots table for periodic proctoring snapshots

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-03-27 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "l2m3n4o5p6q7"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "interview_screenshots" not in inspector.get_table_names():
        op.create_table(
            "interview_screenshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "session_id",
                sa.Integer(),
                sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("file_path", sa.String(500), nullable=False),
            sa.Column(
                "captured_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
    if "ix_interview_screenshots_session_id" not in [
        idx["name"] for idx in inspector.get_indexes("interview_screenshots")
    ]:
        op.create_index(
            "ix_interview_screenshots_session_id",
            "interview_screenshots",
            ["session_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_interview_screenshots_session_id", table_name="interview_screenshots")
    op.drop_table("interview_screenshots")
