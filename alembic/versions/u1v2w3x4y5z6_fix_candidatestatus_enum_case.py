"""Fix candidatestatus enum case: Selected->selected, Hired->hired

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-03-29 00:00:00.000000
"""
from alembic import op

# revision identifiers
revision = "u1v2w3x4y5z6"
down_revision = "t0u1v2w3x4y5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL requires recreating the enum type to rename values.
    # Steps: rename old type, create new type with correct values,
    # migrate columns, drop old type.

    # 1. Rename the existing enum type
    op.execute("ALTER TYPE candidatestatus RENAME TO candidatestatus_old")

    # 2. Create new enum type with all-lowercase values (matching Python enum .name)
    op.execute(
        "CREATE TYPE candidatestatus AS ENUM "
        "('applied', 'interview', 'offered', 'selected', 'hired', 'rejected')"
    )

    # 3. Migrate the column: cast through text to handle the case change
    op.execute(
        "ALTER TABLE candidates "
        "ALTER COLUMN status TYPE candidatestatus "
        "USING (lower(status::text)::candidatestatus)"
    )

    # 4. Drop the old enum type
    op.execute("DROP TYPE candidatestatus_old")


def downgrade() -> None:
    # Reverse: restore mixed-case enum values
    op.execute("ALTER TYPE candidatestatus RENAME TO candidatestatus_old")
    op.execute(
        "CREATE TYPE candidatestatus AS ENUM "
        "('applied', 'interview', 'offered', 'Selected', 'Hired', 'rejected')"
    )
    op.execute(
        "ALTER TABLE candidates "
        "ALTER COLUMN status TYPE candidatestatus "
        "USING ("
        "  CASE status::text "
        "    WHEN 'selected' THEN 'Selected'::candidatestatus "
        "    WHEN 'hired' THEN 'Hired'::candidatestatus "
        "    ELSE status::text::candidatestatus "
        "  END"
        ")"
    )
    op.execute("DROP TYPE candidatestatus_old")
