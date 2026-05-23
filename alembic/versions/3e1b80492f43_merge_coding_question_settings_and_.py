"""merge coding_question_settings and observer_tokens heads

Revision ID: 3e1b80492f43
Revises: a4b5c6d7e8f0, e7f8a9b0c1d2
Create Date: 2026-05-23 22:51:03.097377

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3e1b80492f43'
down_revision: Union[str, None] = ('a4b5c6d7e8f0', 'e7f8a9b0c1d2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
