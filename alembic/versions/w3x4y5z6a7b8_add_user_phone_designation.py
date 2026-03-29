"""Add phone and designation to users table

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-03-29 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "w3x4y5z6a7b8"
down_revision = "v2w3x4y5z6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("designation", sa.String(150), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "designation")
    op.drop_column("users", "phone")
