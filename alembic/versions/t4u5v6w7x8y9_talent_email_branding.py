"""Talent Agent: per-organization email branding on talent_settings

Revision ID: t4u5v6w7x8y9
Revises: s3t4u5v6w7x8
Create Date: 2026-10-12 12:00:00.000000

Adds nullable columns only. Existing settings rows keep their refresh
interval; the 90-day default applies to organizations set up from now on.
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "t4u5v6w7x8y9"
down_revision = "s3t4u5v6w7x8"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("brand_name", sa.String(120), nullable=True),
    sa.Column("brand_logo_url", sa.String(500), nullable=True),
    sa.Column("brand_color", sa.String(7), nullable=True),
    sa.Column("email_signature", sa.String(300), nullable=True),
    sa.Column("refresh_message", sa.Text(), nullable=True),
)


def upgrade() -> None:
    for column in _COLUMNS:
        add_column_if_missing("talent_settings", column.copy())


def downgrade() -> None:
    from alembic import op

    for column in reversed(_COLUMNS):
        if has_column("talent_settings", column.name):
            op.drop_column("talent_settings", column.name)
