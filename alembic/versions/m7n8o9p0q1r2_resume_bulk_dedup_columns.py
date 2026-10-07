"""Add resume_metadata.file_hash / batch_id / parse_mode

Bulk resume uploads kept their queue and progress only in memory, so a
restart mid-upload left rows "pending" forever and the upload screen spinning.
batch_id + parse_mode let the server re-queue unfinished rows on startup and
report a batch's progress from the database. file_hash (SHA-256 of the bytes)
lets a re-upload of the identical file replace the older copy even when no
email can be read from it.

The same columns and indexes are also ensured by startup.py and the raw DDL
list in app.main, so this migration is guarded against both having run first.

Revision ID: m7n8o9p0q1r2
Revises: l6m7n8o9p0q1
Create Date: 2026-10-07 10:00:00.000000
"""
import sqlalchemy as sa

from app.migration_helpers import add_column_if_missing, create_index_if_missing, has_column, has_index

# revision identifiers
revision = "m7n8o9p0q1r2"
down_revision = "l6m7n8o9p0q1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("resume_metadata", sa.Column("file_hash", sa.String(64), nullable=True))
    add_column_if_missing("resume_metadata", sa.Column("batch_id", sa.String(32), nullable=True))
    add_column_if_missing("resume_metadata", sa.Column("parse_mode", sa.String(10), nullable=True))
    create_index_if_missing("ix_resume_metadata_file_hash", "resume_metadata", ["file_hash"])
    create_index_if_missing("ix_resume_metadata_batch_id", "resume_metadata", ["batch_id"])


def downgrade() -> None:
    from alembic import op

    for index in ("ix_resume_metadata_batch_id", "ix_resume_metadata_file_hash"):
        if has_index("resume_metadata", index):
            op.drop_index(index, table_name="resume_metadata")
    for column in ("parse_mode", "batch_id", "file_hash"):
        if has_column("resume_metadata", column):
            op.drop_column("resume_metadata", column)
