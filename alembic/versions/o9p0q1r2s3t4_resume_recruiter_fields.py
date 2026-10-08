"""Add resume_metadata.recruiter_fields

The candidate profile labels each fact "From CV" or "Recruiter". This JSON
list names the fields whose current value a recruiter typed in; the
before_flush hook in app.models.resume_metadata maintains it (a recruiter edit
adds a field, a later CV write takes it back).

Also ensured by startup.py and the raw DDL list in app.main.

Revision ID: o9p0q1r2s3t4
Revises: n8o9p0q1r2s3
Create Date: 2026-10-08 10:00:00.000000
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "o9p0q1r2s3t4"
down_revision = "n8o9p0q1r2s3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("resume_metadata", sa.Column("recruiter_fields", sa.Text(), nullable=True))


def downgrade() -> None:
    from alembic import op

    if has_column("resume_metadata", "recruiter_fields"):
        op.drop_column("resume_metadata", "recruiter_fields")
