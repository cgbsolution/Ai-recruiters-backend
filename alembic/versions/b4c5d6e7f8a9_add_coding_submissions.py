"""Add coding_submissions table and evaluations.coding_score

Revision ID: b4c5d6e7f8a9
Revises: z3a4b5c6d7e8
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "b4c5d6e7f8a9"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = inspect(bind)

    # 1. coding_submissions table — guarded so re-runs are safe.
    if "coding_submissions" not in insp.get_table_names():
        op.create_table(
            "coding_submissions",
            sa.Column("id", sa.Integer, primary_key=True, index=True),
            sa.Column(
                "session_id",
                sa.Integer,
                sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("question_index", sa.Integer, nullable=True),
            sa.Column("title", sa.String(255), nullable=True),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("difficulty", sa.String(20), nullable=True),
            sa.Column("language", sa.String(40), nullable=False),
            sa.Column("code", sa.Text, nullable=False),
            sa.Column("test_cases_total", sa.Integer, nullable=False, server_default="0"),
            sa.Column("test_cases_passed", sa.Integer, nullable=False, server_default="0"),
            sa.Column("test_results", sa.Text, nullable=True),
            sa.Column("execution_time_ms", sa.Integer, nullable=True),
            sa.Column(
                "submitted_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # 2. evaluations.coding_score column
    eval_cols = [c["name"] for c in insp.get_columns("evaluations")]
    if "coding_score" not in eval_cols:
        op.add_column(
            "evaluations",
            sa.Column("coding_score", sa.Float, nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    insp = inspect(bind)

    eval_cols = [c["name"] for c in insp.get_columns("evaluations")]
    if "coding_score" in eval_cols:
        op.drop_column("evaluations", "coding_score")

    if "coding_submissions" in insp.get_table_names():
        op.drop_table("coding_submissions")
