"""Widen candidate columns that startup.py created too narrow

Revision ID: g1h2i3j4k5l6
Revises: f1a2b3c4d5e6
Create Date: 2026-08-28 00:00:00.000000

`startup.py` used to run before `alembic upgrade head`, so on every existing
deployment these two columns were created from its (narrower) DDL and the
migration that would have created them wider was skipped:

    interview_timezone    VARCHAR(50)  -- model and migration say 100
    joining_flexibility   VARCHAR(20)  -- model and migration say 30

A timezone label such as "(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi" is
51 characters, so writes were failing with StringDataRightTruncation on the
databases that took the startup.py path while passing on a freshly migrated
one. Widening is safe and lossless.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.migration_helpers import has_column


revision: str = 'g1h2i3j4k5l6'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, target length)
WIDENED = [
    ('candidates', 'interview_timezone', 100),
    ('candidates', 'joining_flexibility', 30),
]


def _current_length(table: str, column: str):
    for col in sa.inspect(op.get_bind()).get_columns(table):
        if col['name'] == column:
            return getattr(col['type'], 'length', None)
    return None


def upgrade() -> None:
    for table, column, length in WIDENED:
        if not has_column(table, column):
            continue
        current = _current_length(table, column)
        if current is not None and current < length:
            op.alter_column(
                table,
                column,
                type_=sa.String(length),
                existing_type=sa.String(current),
                existing_nullable=True,
            )


def downgrade() -> None:
    # Narrowing again could truncate live data; the wider type is correct.
    pass
