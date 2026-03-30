"""Add Google OAuth fields to users table

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "a4b5c6d7e8f9"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    insp = inspect(op.get_bind())
    cols = [c["name"] for c in insp.get_columns("users")]
    if "google_access_token" not in cols:
        op.add_column("users", sa.Column("google_access_token", sa.Text, nullable=True))
    if "google_refresh_token" not in cols:
        op.add_column("users", sa.Column("google_refresh_token", sa.Text, nullable=True))
    if "google_email" not in cols:
        op.add_column("users", sa.Column("google_email", sa.String(255), nullable=True))
    if "google_connected_at" not in cols:
        op.add_column("users", sa.Column("google_connected_at", sa.DateTime, nullable=True))


def downgrade():
    op.drop_column("users", "google_connected_at")
    op.drop_column("users", "google_email")
    op.drop_column("users", "google_refresh_token")
    op.drop_column("users", "google_access_token")
