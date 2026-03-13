"""Add Selected and Hired to candidatestatus enum

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-03-13 02:00:00.000000
"""
from alembic import op

# revision identifiers
revision = "f6g7h8i9j0k1"
down_revision = "e5f6g7h8i9j0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new enum values to the candidatestatus PostgreSQL enum type
    op.execute("ALTER TYPE candidatestatus ADD VALUE IF NOT EXISTS 'Selected'")
    op.execute("ALTER TYPE candidatestatus ADD VALUE IF NOT EXISTS 'Hired'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values easily
    pass
