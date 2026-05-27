"""helios_exclude tri-state nullable

Revision ID: 892742dc301d
Revises: c9f4b8a1d3e2
Create Date: 2026-05-15 14:40:00.000000

Phase 6 Track 6A — converts ``meetings.helios_exclude`` from
``BOOLEAN NOT NULL DEFAULT false`` to a tri-state nullable column per
``HELIOS_BUILD_PLAN.md`` lines 2210-2216 (Option C):

* ``NULL``   → fall back to keyword / manual exclusion (default)
* ``TRUE``   → always exclude (user explicit opt-out)
* ``FALSE``  → always include (user explicit override of keyword match)

The earlier ``f175f17074af`` migration created the column as NOT NULL.
Rather than rewrite an applied migration, we adjust here so dev DBs
already at head migrate forward cleanly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "892742dc301d"
down_revision: Union[str, Sequence[str], None] = "c9f4b8a1d3e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop NOT NULL + server default; convert existing ``false`` rows to NULL.

    Existing rows are all ``false`` because that was the previous default;
    treating them as NULL restores the "use keyword logic" fall-through
    that Option C expects (no row has actually been explicitly toggled
    yet — the dashboard UI that writes this column ships in Wave 2).
    """
    op.alter_column(
        "meetings",
        "helios_exclude",
        existing_type=sa.Boolean(),
        nullable=True,
        server_default=None,
    )
    # Reset legacy ``false`` rows to NULL so the tri-state semantics hold.
    op.execute("UPDATE meetings SET helios_exclude = NULL WHERE helios_exclude = false")


def downgrade() -> None:
    """Restore NOT NULL + default false (matches ``f175f17074af``)."""
    op.execute("UPDATE meetings SET helios_exclude = false WHERE helios_exclude IS NULL")
    op.alter_column(
        "meetings",
        "helios_exclude",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    )
