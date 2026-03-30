"""Add allow_code_editor to organizations

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "y2z3a4b5c6d7"
down_revision = "x1y2z3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    insp = inspect(op.get_bind())
    cols = [c["name"] for c in insp.get_columns("organizations")]
    if "allow_code_editor" not in cols:
        op.add_column(
            "organizations",
            sa.Column("allow_code_editor", sa.Boolean, nullable=False, server_default="false"),
        )


def downgrade():
    op.drop_column("organizations", "allow_code_editor")
