"""Talent Agent schema

Adds freshness / identity columns to resume_metadata (the talent pool) and
creates the Talent Agent tables: consent, suppression, CV versions, campaigns,
touches, send caps, deliverability events, magic-link tokens, job settings,
matches, approvals, interviews, bookings, batch runs and the audit log.

Purely additive — no existing row or column is changed. resume_metadata gets
`verification_status` with a constant default (instant on PostgreSQL 11+).

The new tables are created from their models (checkfirst), so a database where
app.main's create_all fallback already made them is left alone. Embedding
columns are `vector(N)` when the pgvector extension can be enabled and
`double precision[]` otherwise (app.talent.vectors) — a server without pgvector
still upgrades cleanly. An AFTER DELETE trigger on resume_metadata writes an
audit_log row for every deleted profile, whatever deleted it (app.talent.schema).

Revision ID: q1r2s3t4u5v6
Revises: p0q1r2s3t4u5
Create Date: 2026-10-09 12:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

from app.migration_helpers import add_column_if_missing, create_index_if_missing, has_column, has_table

# revision identifiers
revision = "q1r2s3t4u5v6"
down_revision = "p0q1r2s3t4u5"
branch_labels = None
depends_on = None

# Dependency order (parents first).
TALENT_TABLES = (
    "talent_settings",
    "contact_consent",
    "suppression_list",
    "cv_versions",
    "campaigns",
    "campaign_members",
    "touches",
    "sender_domains",
    "send_counters",
    "deliverability_events",
    "talent_access_tokens",
    "talent_job_settings",
    "job_matches",
    "talent_approvals",
    "talent_interviews",
    "talent_interview_bookings",
    "talent_batch_runs",
    "audit_log",
)

RESUME_COLUMNS = (
    sa.Column("candidate_fields", sa.Text(), nullable=True),
    sa.Column("last_verified_at", sa.DateTime(), nullable=True),
    sa.Column("verification_status", sa.String(20), nullable=False, server_default="unverified"),
    sa.Column("job_search_status", sa.String(20), nullable=True),
    sa.Column("dedupe_key", sa.String(64), nullable=True),
    sa.Column("email_status", sa.String(20), nullable=True),
    sa.Column("email_checked_at", sa.DateTime(), nullable=True),
    sa.Column("retention_until", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    import app.models  # noqa: F401 — registers every table on Base.metadata
    from app.database import Base
    from app.talent.schema import ensure_db_objects

    bind = op.get_bind()

    for column in RESUME_COLUMNS:
        add_column_if_missing("resume_metadata", column)
    create_index_if_missing("ix_resume_metadata_verification_status", "resume_metadata", ["verification_status"])
    create_index_if_missing("ix_resume_metadata_dedupe_key", "resume_metadata", ["dedupe_key"])

    for name in TALENT_TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)

    ensure_db_objects(bind)


def downgrade() -> None:
    from app.talent.schema import drop_db_objects

    drop_db_objects(op.get_bind())
    for name in reversed(TALENT_TABLES):
        if has_table(name):
            op.drop_table(name)
    for column in RESUME_COLUMNS:
        if has_column("resume_metadata", column.name):
            op.drop_column("resume_metadata", column.name)
