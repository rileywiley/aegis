"""voice_note_extraction_links

Revision ID: c9f4b8a1d3e2
Revises: 28ff6a6b6e83
Create Date: 2026-05-02 00:00:00.000000

Per HELIOS.md §16.12 — extraction-pipeline support for voice notes (Wave 4L).

Two changes, both small:

1. ``action_items.source_voice_note_id`` — voice notes can produce action
   items the same way meetings, emails, and chats do. Add a nullable FK
   so the extraction pipeline can record the source. Matches the existing
   ``source_meeting_id`` / ``source_email_id`` / ``source_chat_message_id``
   pattern. ON DELETE SET NULL keeps action items alive if the voice note
   is later deleted (mirrors the email/chat behavior).

2. ``workstream_items`` check constraint — extend the allowed
   ``item_type`` values to include ``'voice_note'`` so the workstream
   detector can link voice notes to workstreams alongside other item
   kinds. The constraint is dropped and re-created in both directions.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c9f4b8a1d3e2'
down_revision: Union[str, Sequence[str], None] = '28ff6a6b6e83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_WS_ITEM_TYPES = (
    "'meeting','email','chat_message','action_item',"
    "'decision','commitment','email_ask','chat_ask'"
)
_NEW_WS_ITEM_TYPES = (
    "'meeting','email','chat_message','action_item',"
    "'decision','commitment','email_ask','chat_ask','voice_note'"
)


def upgrade() -> None:
    # 1. action_items.source_voice_note_id
    op.add_column(
        'action_items',
        sa.Column('source_voice_note_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_action_items_source_voice_note',
        'action_items',
        'voice_notes',
        ['source_voice_note_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # 2. Extend workstream_items.item_type CHECK to accept 'voice_note'.
    op.drop_constraint(
        'ck_workstream_items_type', 'workstream_items', type_='check'
    )
    op.create_check_constraint(
        'ck_workstream_items_type',
        'workstream_items',
        f"item_type IN ({_NEW_WS_ITEM_TYPES})",
    )


def downgrade() -> None:
    # Reverse order from upgrade.
    op.drop_constraint(
        'ck_workstream_items_type', 'workstream_items', type_='check'
    )
    op.create_check_constraint(
        'ck_workstream_items_type',
        'workstream_items',
        f"item_type IN ({_OLD_WS_ITEM_TYPES})",
    )

    op.drop_constraint(
        'fk_action_items_source_voice_note',
        'action_items',
        type_='foreignkey',
    )
    op.drop_column('action_items', 'source_voice_note_id')
