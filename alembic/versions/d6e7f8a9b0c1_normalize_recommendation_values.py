"""Normalize evaluations.recommendation legacy values.

Older rows were written with the Python enum .name ("hire",
"strong_hire", "no_hire", "maybe") before values_callable was switched
to .value. Those rows now crash on read with LookupError. This migration
rewrites them to the canonical title-case labels.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-04-23 00:00:00.000000
"""
from alembic import op


revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # If the enum type is missing any of the legacy lowercase/underscore
        # labels the UPDATE below would be a no-op anyway (Postgres would have
        # rejected those writes at INSERT time). If the enum *does* contain
        # them — e.g. someone ran ALTER TYPE — cast through text so the
        # comparison works regardless.
        op.execute(
            """
            UPDATE evaluations
            SET recommendation = CASE LOWER(recommendation::text)
                WHEN 'strong_hire' THEN 'Strong Hire'
                WHEN 'strong hire' THEN 'Strong Hire'
                WHEN 'hire' THEN 'Hire'
                WHEN 'maybe' THEN 'Maybe'
                WHEN 'no_hire' THEN 'No Hire'
                WHEN 'no hire' THEN 'No Hire'
                ELSE recommendation::text
            END::recommendation
            WHERE recommendation IS NOT NULL
              AND recommendation::text NOT IN ('Strong Hire','Hire','Maybe','No Hire')
            """
        )
    else:
        op.execute(
            """
            UPDATE evaluations
            SET recommendation = CASE LOWER(recommendation)
                WHEN 'strong_hire' THEN 'Strong Hire'
                WHEN 'strong hire' THEN 'Strong Hire'
                WHEN 'hire' THEN 'Hire'
                WHEN 'maybe' THEN 'Maybe'
                WHEN 'no_hire' THEN 'No Hire'
                WHEN 'no hire' THEN 'No Hire'
                ELSE recommendation
            END
            WHERE recommendation IS NOT NULL
              AND recommendation NOT IN ('Strong Hire','Hire','Maybe','No Hire')
            """
        )


def downgrade() -> None:
    # Data normalization is not reversible — we don't know which rows were
    # originally lowercase. No-op.
    pass
