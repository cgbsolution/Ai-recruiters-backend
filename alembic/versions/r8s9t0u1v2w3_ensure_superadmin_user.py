"""Ensure info@cgbindia.com is always super_admin

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-03-29 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "r8s9t0u1v2w3"
down_revision = "q7r8s9t0u1v2"
branch_labels = None
depends_on = None

SUPER_ADMIN_EMAIL = "info@cgbindia.com"


def upgrade():
    op.execute(
        sa.text(
            "UPDATE users SET role = 'super_admin' WHERE email = :email"
        ).bindparams(email=SUPER_ADMIN_EMAIL)
    )


def downgrade():
    # No-op: we don't want to accidentally demote on downgrade
    pass
