"""Add missing columns to evaluations table

Revision ID: x1y2z3a4b5c6
Revises: w3x4y5z6a7b8
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "x1y2z3a4b5c6"
down_revision = "w3x4y5z6a7b8"
branch_labels = None
depends_on = None


def _col_exists(table, col):
    insp = inspect(op.get_bind())
    return col in [c["name"] for c in insp.get_columns(table)]


def upgrade():
    cols = [
        ("per_question_evaluations", sa.Text),
        ("skills_ratings", sa.Text),
        ("meets_threshold", sa.Boolean),
        ("pronunciation_score", sa.Float),
        ("enthusiasm_score", sa.Float),
        ("grammar_score", sa.Float),
        ("conciseness_score", sa.Float),
        ("clarity_score", sa.Float),
    ]
    for col_name, col_type in cols:
        if not _col_exists("evaluations", col_name):
            op.add_column("evaluations", sa.Column(col_name, col_type, nullable=True))

    # interview_screenshots.face_count
    insp = inspect(op.get_bind())
    if "interview_screenshots" in insp.get_table_names():
        if not _col_exists("interview_screenshots", "face_count"):
            op.add_column("interview_screenshots", sa.Column("face_count", sa.Integer, nullable=True))


def downgrade():
    pass
