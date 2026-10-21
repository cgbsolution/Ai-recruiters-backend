"""AskMira connections per organization, and their one-time connect codes

Revision ID: c8m3o4t5c6n7
Revises: b7l2n3s4b5m6
Create Date: 2026-10-21 18:00:00.000000

An admin of an organization allows AskMira's connect request; the organization
then has a secret of its own. Replaces the server-wide connection key
(askmira_connection, left in place, no longer read). Additive only — safe on a
live database.
"""
import sqlalchemy as sa

from app.migration_helpers import create_index_if_missing, create_table_if_missing, has_table

# revision identifiers
revision = "c8m3o4t5c6n7"
down_revision = "b7l2n3s4b5m6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "askmira_org_connections",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("secret_nonce", sa.String(64), nullable=False),
        sa.Column("askmira_name", sa.String(200), nullable=True),
        sa.Column("askmira_origin", sa.String(300), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("connected_by_user_id", sa.Integer(), nullable=True),
    )
    create_table_if_missing(
        "askmira_connect_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("askmira_tenant_id", sa.String(36), nullable=False),
        sa.Column("askmira_name", sa.String(200), nullable=True),
        sa.Column("askmira_origin", sa.String(300), nullable=True),
        sa.Column("challenge", sa.String(128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    create_index_if_missing(
        "ix_askmira_connect_codes_expires_at", "askmira_connect_codes", ["expires_at"]
    )


def downgrade() -> None:
    from alembic import op

    for table in ("askmira_connect_codes", "askmira_org_connections"):
        if has_table(table):
            op.drop_table(table)
