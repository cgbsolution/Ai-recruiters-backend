"""Add max_jobs and max_candidates_per_job quota columns to organizations

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-03-28 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "p6q7r8s9t0u1"
down_revision = "o5p6q7r8s9t0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing_cols = [c["name"] for c in inspector.get_columns("organizations")]

    if "max_jobs" not in existing_cols:
        op.add_column(
            "organizations",
            sa.Column("max_jobs", sa.Integer(), nullable=False, server_default="0"),
        )
    if "max_candidates_per_job" not in existing_cols:
        op.add_column(
            "organizations",
            sa.Column("max_candidates_per_job", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing_cols = [c["name"] for c in inspector.get_columns("organizations")]

    if "max_candidates_per_job" in existing_cols:
        op.drop_column("organizations", "max_candidates_per_job")
    if "max_jobs" in existing_cols:
        op.drop_column("organizations", "max_jobs")
