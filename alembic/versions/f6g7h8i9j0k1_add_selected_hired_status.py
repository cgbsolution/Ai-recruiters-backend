"""Add Selected and Hired to candidatestatus enum

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-03-13 02:00:00.000000
"""
from alembic import op

from app.migration_helpers import enum_labels, has_enum_type

# revision identifiers
revision = "f6g7h8i9j0k1"
down_revision = "e5f6g7h8i9j0"
branch_labels = None
depends_on = None

NEW_VALUES = ("Selected", "Hired")


def upgrade() -> None:
    # Only touch the type when a label is genuinely missing. ALTER TYPE ... ADD
    # VALUE cannot run inside a transaction block on PostgreSQL < 12, and
    # Alembic wraps the whole upgrade in one — so skipping the statement
    # entirely on databases that already have these labels (u1v2w3x4y5z6 later
    # rebuilds the type in lowercase anyway) keeps the upgrade replayable.
    if not has_enum_type("candidatestatus"):
        return
    existing = enum_labels("candidatestatus")
    for value in NEW_VALUES:
        if value not in existing and value.lower() not in existing:
            op.execute(f"ALTER TYPE candidatestatus ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values easily
    pass
