"""Add allow_whatsapp to organizations

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "z3a4b5c6d7e8"
down_revision = "y2z3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    insp = inspect(op.get_bind())
    cols = [c["name"] for c in insp.get_columns("organizations")]
    if "allow_whatsapp" not in cols:
        op.add_column(
            "organizations",
            sa.Column("allow_whatsapp", sa.Boolean, nullable=False, server_default="false"),
        )


def downgrade():
    op.drop_column("organizations", "allow_whatsapp")
