"""Change resume_metadata unique constraint from email-only to email+org

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-03-29 16:00:00.000000
"""
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "t0u1v2w3x4y5"
down_revision = "s9t0u1v2w3x4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)

    # Drop the old unique constraint on candidate_email alone
    existing_uq = [c["name"] for c in inspector.get_unique_constraints("resume_metadata")]

    # The auto-generated unique constraint name varies by DB
    for uq_name in existing_uq:
        if "candidate_email" in str(inspector.get_unique_constraints("resume_metadata")):
            try:
                op.drop_constraint(uq_name, "resume_metadata", type_="unique")
            except Exception:
                pass

    # Create composite unique constraint (email + org)
    if "uq_resume_email_org" not in existing_uq:
        op.create_unique_constraint(
            "uq_resume_email_org",
            "resume_metadata",
            ["candidate_email", "organization_id"],
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing_uq = [c["name"] for c in inspector.get_unique_constraints("resume_metadata")]

    if "uq_resume_email_org" in existing_uq:
        op.drop_constraint("uq_resume_email_org", "resume_metadata", type_="unique")

    # Re-add the old unique constraint
    op.create_unique_constraint(
        "uq_resume_metadata_candidate_email",
        "resume_metadata",
        ["candidate_email"],
    )
