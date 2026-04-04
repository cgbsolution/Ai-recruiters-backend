"""merge branches

Revision ID: c0eef1d7bf45
Revises: d5e6f7g8h9i0, r9s0t1u2v3w4
Create Date: 2026-04-04 12:37:58.920082

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0eef1d7bf45'
down_revision: Union[str, None] = ('d5e6f7g8h9i0', 'r9s0t1u2v3w4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
