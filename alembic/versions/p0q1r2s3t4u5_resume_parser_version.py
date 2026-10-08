"""Add resume_metadata.parser_version

Records which version of the regex parser (fast_resume_parser.
FAST_PARSER_VERSION) produced a "fast" parse. A bulk re-upload of an identical
file reuses the stored parse only when it was made by the current parser, so
parser fixes (version 2: real locations instead of sentence fragments) reach
re-uploaded resumes.

Also ensured by startup.py and the raw DDL list in app.main.

Revision ID: p0q1r2s3t4u5
Revises: o9p0q1r2s3t4
Create Date: 2026-10-08 12:00:00.000000
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, has_column

# revision identifiers
revision = "p0q1r2s3t4u5"
down_revision = "o9p0q1r2s3t4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("resume_metadata", sa.Column("parser_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    from alembic import op

    if has_column("resume_metadata", "parser_version"):
        op.drop_column("resume_metadata", "parser_version")
