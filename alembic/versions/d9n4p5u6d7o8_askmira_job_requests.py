"""Job openings AskMira asked for, by its idempotency key

Revision ID: d9n4p5u6d7o8
Revises: c8m3o4t5c6n7
Create Date: 2026-11-03 12:00:00.000000

AskMira opens the positions an order win needs (POST /integrations/askmira/act,
kind job.open). Each request carries a key; asking again with it returns the
same job. Additive only — safe on a live database.
"""
import sqlalchemy as sa

from app.migration_helpers import create_index_if_missing, create_table_if_missing, has_table

# revision identifiers
revision = "d9n4p5u6d7o8"
down_revision = "c8m3o4t5c6n7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "askmira_job_requests",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("request_key", sa.String(120), primary_key=True),
        sa.Column(
            "job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=True),
    )
    create_index_if_missing(
        "ix_askmira_job_requests_job_id", "askmira_job_requests", ["job_id"]
    )


def downgrade() -> None:
    from alembic import op

    if has_table("askmira_job_requests"):
        op.drop_table("askmira_job_requests")
