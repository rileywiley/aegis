"""add_meeting_helios_exclude

Revision ID: f175f17074af
Revises: a1b2c3d4e5f6
Create Date: 2026-05-03 21:19:50.185952

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f175f17074af'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add helios_exclude boolean column to meetings.

    Per HELIOS.md §16.6 — used by the per-meeting override from the
    dashboard Calendar page. Default false so existing rows backfill cleanly.
    """
    op.add_column(
        "meetings",
        sa.Column(
            "helios_exclude",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Remove helios_exclude column from meetings."""
    op.drop_column("meetings", "helios_exclude")
