"""Add allow_resume_database permission to organizations

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-03-29 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "s9t0u1v2w3x4"
down_revision = "r8s9t0u1v2w3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing_cols = [c["name"] for c in inspector.get_columns("organizations")]

    if "allow_resume_database" not in existing_cols:
        op.add_column(
            "organizations",
            sa.Column("allow_resume_database", sa.Boolean(), nullable=False, server_default="false"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing_cols = [c["name"] for c in inspector.get_columns("organizations")]

    if "allow_resume_database" in existing_cols:
        op.drop_column("organizations", "allow_resume_database")
