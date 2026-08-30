"""Add password reset token columns to users

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-08-29 00:00:00.000000

Backs the forgot-password flow: POST /auth/forgot-password stores a one-time
token plus its expiry here, and POST /auth/reset-password clears both once the
password has actually been changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column


revision: str = 'i3j4k5l6m7n8'
down_revision: Union[str, None] = 'h2i3j4k5l6m7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMNS = ('password_reset_token', 'password_reset_expires')


def upgrade() -> None:
    add_column_if_missing('users', sa.Column('password_reset_token', sa.String(128), nullable=True))
    add_column_if_missing('users', sa.Column('password_reset_expires', sa.DateTime(), nullable=True))


def downgrade() -> None:
    for column in reversed(COLUMNS):
        if has_column('users', column):
            op.drop_column('users', column)
