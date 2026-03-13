"""Add resume_metadata and job_metadata tables + candidate.parsing_status/resume_metadata_id

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-03-11 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "e5f6g7h8i9j0"
down_revision = "d4e5f6g7h8i9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── resume_metadata table ────────────────────────────────────────────────
    op.create_table(
        "resume_metadata",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_email", sa.String(255), nullable=False, unique=True, index=True),
        sa.Column("candidate_name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("current_designation", sa.String(255), nullable=True),
        sa.Column("experience_years", sa.Float(), nullable=True),
        sa.Column("skills", sa.Text(), nullable=True),
        sa.Column("education", sa.Text(), nullable=True),
        sa.Column("certifications", sa.Text(), nullable=True),
        sa.Column("work_experience", sa.Text(), nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("personal_details", sa.Text(), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("resume_file_path", sa.String(500), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("parsing_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("parsing_error", sa.Text(), nullable=True),
        sa.Column("parsed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── job_metadata table ───────────────────────────────────────────────────
    op.create_table(
        "job_metadata",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False, unique=True, index=True),
        sa.Column("parsed_title", sa.String(255), nullable=True),
        sa.Column("required_skills", sa.Text(), nullable=True),
        sa.Column("experience_range", sa.String(100), nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("education_requirements", sa.Text(), nullable=True),
        sa.Column("designation", sa.String(255), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("responsibilities_summary", sa.Text(), nullable=True),
        sa.Column("raw_jd_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── New columns on candidates ────────────────────────────────────────────
    op.add_column("candidates", sa.Column("resume_metadata_id", sa.Integer(), nullable=True))
    op.add_column("candidates", sa.Column("parsing_status", sa.String(20), nullable=True))
    op.create_foreign_key(
        "fk_candidates_resume_metadata",
        "candidates",
        "resume_metadata",
        ["resume_metadata_id"],
        ["id"],
    )
    op.create_index("ix_candidates_resume_metadata_id", "candidates", ["resume_metadata_id"])


def downgrade() -> None:
    op.drop_index("ix_candidates_resume_metadata_id", table_name="candidates")
    op.drop_constraint("fk_candidates_resume_metadata", "candidates", type_="foreignkey")
    op.drop_column("candidates", "parsing_status")
    op.drop_column("candidates", "resume_metadata_id")
    op.drop_table("job_metadata")
    op.drop_table("resume_metadata")
