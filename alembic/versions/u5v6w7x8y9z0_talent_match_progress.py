"""Talent Agent: live matching progress and the worker heartbeat

Revision ID: u5v6w7x8y9z0
Revises: t4u5v6w7x8y9
Create Date: 2026-10-13 10:00:00.000000

Adds talent_job_settings.match_progress and the talent_agent_heartbeats
table. Additive only.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "u5v6w7x8y9z0"
down_revision = "t4u5v6w7x8y9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    add_column_if_missing("talent_job_settings", sa.Column("match_progress", JSONB(), nullable=True))
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("talent_agent_heartbeats"):
        op.create_table(
            "talent_agent_heartbeats",
            sa.Column("name", sa.String(40), primary_key=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    from alembic import op

    if sa.inspect(op.get_bind()).has_table("talent_agent_heartbeats"):
        op.drop_table("talent_agent_heartbeats")
    if has_column("talent_job_settings", "match_progress"):
        op.drop_column("talent_job_settings", "match_progress")
