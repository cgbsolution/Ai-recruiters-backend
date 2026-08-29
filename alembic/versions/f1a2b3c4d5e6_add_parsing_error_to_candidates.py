"""add parsing_error to candidates

Revision ID: f1a2b3c4d5e6
Revises: 3e1b80492f43
Create Date: 2026-06-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = '3e1b80492f43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    add_column_if_missing('candidates', sa.Column('parsing_error', sa.Text(), nullable=True))


def downgrade() -> None:
    if has_column('candidates', 'parsing_error'):
        op.drop_column('candidates', 'parsing_error')
