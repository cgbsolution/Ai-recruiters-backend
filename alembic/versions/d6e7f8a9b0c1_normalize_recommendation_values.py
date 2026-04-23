"""Normalize evaluations.recommendation legacy values.

The Postgres `recommendation` enum type drifted out of sync with the
Python enum across earlier migrations: some deployments ended up with
lowercase/underscore labels (hire, strong_hire, no_hire) and cannot
accept title-case writes ("No Hire"), while newer code reads title-case.

Rather than recreate the enum and keep fighting the drift, convert the
column to plain varchar in one shot, normalizing values during the
conversion. The Python side already uses a TypeDecorator
(RecommendationType) with impl=String(32), so this also removes the
DDL/model mismatch.

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
        # Convert the column off the enum type and normalize data at the
        # same time. The USING clause transforms every existing value
        # (whether stored as "hire", "strong_hire", "No Hire", etc.) to
        # its canonical title-case form.
        op.execute(
            """
            ALTER TABLE evaluations
            ALTER COLUMN recommendation TYPE varchar(32)
            USING (
                CASE LOWER(recommendation::text)
                    WHEN 'strong_hire' THEN 'Strong Hire'
                    WHEN 'strong hire' THEN 'Strong Hire'
                    WHEN 'hire' THEN 'Hire'
                    WHEN 'maybe' THEN 'Maybe'
                    WHEN 'no_hire' THEN 'No Hire'
                    WHEN 'no hire' THEN 'No Hire'
                    ELSE recommendation::text
                END
            )
            """
        )
        # The enum type is no longer referenced by any column; drop it so
        # future fresh-DB setups don't recreate the drift.
        op.execute("DROP TYPE IF EXISTS recommendation")
    else:
        # MySQL / SQLite: column is already effectively VARCHAR.
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
    # Forwards-only: we would not recover the exact legacy enum contents,
    # and the column is varchar going forward by design.
    pass
