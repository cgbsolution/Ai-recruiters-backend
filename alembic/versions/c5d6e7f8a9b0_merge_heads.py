"""Merge divergent migration heads (orphaned branch + main chain)

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9, c0eef1d7bf45
Create Date: 2026-04-15

Background
----------
Two head revisions have been co-existing:
  - b4c5d6e7f8a9 (main chain, latest: add_coding_submissions)
  - c0eef1d7bf45 (an older merge point whose children never got built on)

`alembic upgrade head` fails with "Multiple head revisions are present" until
we unify them. This is a no-op merge — it only updates the revision graph.
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision = "c5d6e7f8a9b0"
down_revision = ("b4c5d6e7f8a9", "c0eef1d7bf45")
branch_labels = None
depends_on = None


def upgrade():
    # No schema changes — pure revision-graph merge.
    pass


def downgrade():
    pass
