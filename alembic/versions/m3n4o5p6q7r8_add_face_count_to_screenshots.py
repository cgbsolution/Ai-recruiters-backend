"""Add face_count column to interview_screenshots for face detection classification

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-03-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "m3n4o5p6q7r8"
down_revision = "l2m3n4o5p6q7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("interview_screenshots")]
    if "face_count" not in columns:
        op.add_column(
            "interview_screenshots",
            sa.Column("face_count", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("interview_screenshots", "face_count")
