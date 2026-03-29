"""Add lifetime usage counters to organizations

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-03-29 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "q7r8s9t0u1v2"
down_revision = "p6q7r8s9t0u1"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing_cols = [c["name"] for c in inspector.get_columns("organizations")]

    if "total_jobs_used" not in existing_cols:
        op.add_column(
            "organizations",
            sa.Column("total_jobs_used", sa.Integer(), nullable=False, server_default="0"),
        )
    if "total_candidates_used" not in existing_cols:
        op.add_column(
            "organizations",
            sa.Column("total_candidates_used", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing_cols = [c["name"] for c in inspector.get_columns("organizations")]

    if "total_candidates_used" in existing_cols:
        op.drop_column("organizations", "total_candidates_used")
    if "total_jobs_used" in existing_cols:
        op.drop_column("organizations", "total_jobs_used")
