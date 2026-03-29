"""Drop old unique index on resume_metadata.candidate_email

The composite unique constraint (candidate_email, organization_id)
replaced the single-column unique index, but the old index was
not dropped — causing UniqueViolation when the same email exists
across different organizations.

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-03-29 07:45:00.000000
"""
from alembic import op

# revision identifiers
revision = "v2w3x4y5z6a7"
down_revision = "u1v2w3x4y5z6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_resume_metadata_candidate_email", table_name="resume_metadata", if_exists=True)
    # Re-create as a non-unique index for query performance
    op.create_index("ix_resume_metadata_candidate_email", "resume_metadata", ["candidate_email"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_resume_metadata_candidate_email", table_name="resume_metadata", if_exists=True)
    op.create_index("ix_resume_metadata_candidate_email", "resume_metadata", ["candidate_email"], unique=True)
