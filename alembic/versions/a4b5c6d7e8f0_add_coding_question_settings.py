"""Add max_coding_questions + manual_coding_questions to interview_settings

Revision ID: a4b5c6d7e8f0
Revises: z3a4b5c6d7e8
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "a4b5c6d7e8f0"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    insp = inspect(op.get_bind())
    cols = [c["name"] for c in insp.get_columns("interview_settings")]

    if "max_coding_questions" not in cols:
        op.add_column(
            "interview_settings",
            sa.Column(
                "max_coding_questions",
                sa.Integer,
                nullable=False,
                server_default="0",
            ),
        )

    if "manual_coding_questions" not in cols:
        op.add_column(
            "interview_settings",
            sa.Column(
                "manual_coding_questions",
                sa.Boolean,
                nullable=False,
                server_default="false",
            ),
        )


def downgrade():
    op.drop_column("interview_settings", "manual_coding_questions")
    op.drop_column("interview_settings", "max_coding_questions")
