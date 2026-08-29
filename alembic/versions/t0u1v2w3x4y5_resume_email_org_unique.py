"""Change resume_metadata unique constraint from email-only to email+org

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-03-29 16:00:00.000000
"""
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.migration_helpers import create_unique_constraint_if_missing, has_table

revision = "t0u1v2w3x4y5"
down_revision = "s9t0u1v2w3x4"
branch_labels = None
depends_on = None


def upgrade():
    if not has_table("resume_metadata"):
        return

    inspector = sa_inspect(op.get_bind())
    constraints = inspector.get_unique_constraints("resume_metadata")

    # Drop only the old email-ONLY unique constraint. Matching on the column
    # list rather than the name matters: the previous version of this
    # migration dropped every unique constraint on the table, which on a
    # re-run silently removed the composite constraint it had just created.
    for uq in constraints:
        if list(uq["column_names"]) == ["candidate_email"]:
            try:
                op.drop_constraint(uq["name"], "resume_metadata", type_="unique")
            except Exception:
                pass

    # Create composite unique constraint (email + org)
    create_unique_constraint_if_missing(
        "uq_resume_email_org",
        "resume_metadata",
        ["candidate_email", "organization_id"],
    )


def downgrade():
    inspector = sa_inspect(op.get_bind())
    existing_uq = [c["name"] for c in inspector.get_unique_constraints("resume_metadata")]

    if "uq_resume_email_org" in existing_uq:
        op.drop_constraint("uq_resume_email_org", "resume_metadata", type_="unique")

    # Re-add the old unique constraint
    create_unique_constraint_if_missing(
        "uq_resume_metadata_candidate_email",
        "resume_metadata",
        ["candidate_email"],
    )
