"""add decision status column

Revision ID: 36444981ccaa
Revises: 5b73bff49bcf
Create Date: 2026-04-28 09:08:23.416995

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '36444981ccaa'
down_revision: Union[str, Sequence[str], None] = '5b73bff49bcf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add status column to decisions table with default 'open'."""
    op.add_column('decisions', sa.Column('status', sa.String(), nullable=True, server_default='open'))
    op.execute("UPDATE decisions SET status = 'open' WHERE status IS NULL")


def downgrade() -> None:
    """Remove status column from decisions table."""
    op.drop_column('decisions', 'status')
