"""Backfill organizations server defaults and add the missing allow_fit_score

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-08-29 00:00:00.000000

Two gaps the multi-tenant migration exposed:

1. `max_jobs` / `max_candidates_per_job` are NOT NULL but the model gave them
   only a Python-side `default=0`, which emits no DDL default. On databases
   created by `create_all()` that made every plain SQL INSERT into
   `organizations` fail. Give them real server defaults (and fill any rows
   that already slipped through as NULL).

2. `Organization.allow_fit_score` exists in the model but no migration ever
   added it, so a database built purely by Alembic would be missing a column
   the application selects on every organization query.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column, has_table


revision: str = 'h2i3j4k5l6m7'
down_revision: Union[str, None] = 'g1h2i3j4k5l6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


QUOTA_COLUMNS = ('max_jobs', 'max_candidates_per_job')


def upgrade() -> None:
    if not has_table('organizations'):
        return

    for column in QUOTA_COLUMNS:
        if not has_column('organizations', column):
            continue
        # Existing NULLs first — SET NOT NULL would reject them, and a default
        # only applies to rows inserted from here on.
        op.execute(f"UPDATE organizations SET {column} = 0 WHERE {column} IS NULL")
        op.execute(f"ALTER TABLE organizations ALTER COLUMN {column} SET DEFAULT 0")
        op.execute(f"ALTER TABLE organizations ALTER COLUMN {column} SET NOT NULL")

    add_column_if_missing(
        'organizations',
        sa.Column('allow_fit_score', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    for column in QUOTA_COLUMNS:
        if has_column('organizations', column):
            op.execute(f"ALTER TABLE organizations ALTER COLUMN {column} DROP DEFAULT")
    if has_column('organizations', 'allow_fit_score'):
        op.drop_column('organizations', 'allow_fit_score')
