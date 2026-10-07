"""Add resume_metadata search / workflow columns

The Resume Database page became a faceted search (experience, CTC, notice
period, city, skills, education, freshness, pipeline status, source, parse
quality). The facts it filters on lived inside JSON columns and the raw text,
so they are now stored as plain derived columns (see
app.services.resume_profile), plus the recruiter's pipeline status and the
resume's source. Existing rows are filled in by resume_profile.backfill_profiles
at startup, not here — migrations must not depend on application code.

The same columns and indexes are also ensured by startup.py and the raw DDL
list in app.main, so this migration is guarded against both having run first.

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
Create Date: 2026-10-07 18:00:00.000000
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, create_index_if_missing, has_column, has_index

# revision identifiers
revision = "n8o9p0q1r2s3"
down_revision = "m7n8o9p0q1r2"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("pipeline_status", sa.String(20), nullable=False, server_default="new"),
    sa.Column("source", sa.String(20), nullable=True),
    sa.Column("current_company", sa.String(255), nullable=True),
    sa.Column("city", sa.String(100), nullable=True),
    sa.Column("highest_education", sa.String(20), nullable=True),
    sa.Column("parse_quality", sa.Integer(), nullable=True),
    sa.Column("skills_index", sa.Text(), nullable=True),
    sa.Column("certs_index", sa.Text(), nullable=True),
    sa.Column("search_text", sa.Text(), nullable=True),
    sa.Column("current_ctc", sa.Float(), nullable=True),
    sa.Column("expected_ctc", sa.Float(), nullable=True),
    sa.Column("notice_period_days", sa.Integer(), nullable=True),
    sa.Column("open_to_relocate", sa.Boolean(), nullable=True),
    sa.Column("derived_version", sa.Integer(), nullable=True),
)
_INDEXES = (
    ("ix_resume_metadata_pipeline_status", ["pipeline_status"]),
    ("ix_resume_metadata_city", ["city"]),
)


def upgrade() -> None:
    for column in _COLUMNS:
        add_column_if_missing("resume_metadata", column.copy())
    for name, columns in _INDEXES:
        create_index_if_missing(name, "resume_metadata", columns)


def downgrade() -> None:
    from alembic import op

    for name, _ in _INDEXES:
        if has_index("resume_metadata", name):
            op.drop_index(name, table_name="resume_metadata")
    for column in reversed(_COLUMNS):
        if has_column("resume_metadata", column.name):
            op.drop_column("resume_metadata", column.name)
