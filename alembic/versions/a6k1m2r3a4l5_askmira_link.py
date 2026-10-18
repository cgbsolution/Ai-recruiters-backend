"""AskMira link: organization/user link columns and used launch tokens

Revision ID: a6k1m2r3a4l5
Revises: u5v6w7x8y9z0
Create Date: 2026-10-16 19:30:00.000000

Adds organizations.askmira_tenant_id (unique), users.askmira_user_id and the
askmira_launches table. Additive only — safe on a live database.
"""
import sqlalchemy as sa

from app.migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
    has_column,
    has_index,
    has_table,
)

# revision identifiers
revision = "a6k1m2r3a4l5"
down_revision = "u5v6w7x8y9z0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("organizations", sa.Column("askmira_tenant_id", sa.String(36), nullable=True))
    create_index_if_missing(
        "ix_organizations_askmira_tenant_id", "organizations", ["askmira_tenant_id"], unique=True
    )
    add_column_if_missing("users", sa.Column("askmira_user_id", sa.String(36), nullable=True))
    create_index_if_missing("ix_users_askmira_user_id", "users", ["askmira_user_id"])
    create_table_if_missing(
        "askmira_launches",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    create_index_if_missing("ix_askmira_launches_expires_at", "askmira_launches", ["expires_at"])


def downgrade() -> None:
    from alembic import op

    if has_table("askmira_launches"):
        op.drop_table("askmira_launches")
    if has_index("users", "ix_users_askmira_user_id"):
        op.drop_index("ix_users_askmira_user_id", table_name="users")
    if has_column("users", "askmira_user_id"):
        op.drop_column("users", "askmira_user_id")
    if has_index("organizations", "ix_organizations_askmira_tenant_id"):
        op.drop_index("ix_organizations_askmira_tenant_id", table_name="organizations")
    if has_column("organizations", "askmira_tenant_id"):
        op.drop_column("organizations", "askmira_tenant_id")
