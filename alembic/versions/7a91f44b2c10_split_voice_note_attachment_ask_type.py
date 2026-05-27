"""split voice_note_attachments target_type='ask' into email_ask/chat_ask

Revision ID: 7a91f44b2c10
Revises: 892742dc301d
Create Date: 2026-05-20 09:30:00.000000

Phase 6 Wave 4 — Critical #4 from bug_report.md.

``EmailAsk`` and ``ChatAsk`` are separate tables with their own SERIAL
sequences, so their numeric IDs can (and will) collide. The original
``target_type='ask'`` value on ``voice_note_attachments`` is therefore
ambiguous: ``list_for_ask(5)`` returns notes attached to BOTH ChatAsk #5
and EmailAsk #5 simultaneously, and the label resolver only queries
``EmailAsk`` so chat-ask attachments degrade to "Ask #N".

This migration splits the type into ``email_ask`` and ``chat_ask``.

Strategy:

* For each row with ``target_type='ask'``:
    1. Check whether ``target_id`` exists in ``email_asks`` — if yes,
       mark as ``email_ask``.
    2. Else check ``chat_asks`` — if yes, mark as ``chat_ask``.
    3. If neither table has the row (orphan), DELETE the attachment.
* If a numeric ID exists in BOTH tables, the migration logs a warning
  and falls back to ``email_ask`` (consistent with the pre-migration
  behaviour where the label resolver also assumed email_asks).
* Tighten the CHECK constraint to disallow the legacy ``ask`` value.
* Downgrade collapses both new types back to ``ask`` (information-losing
  but symmetric and safe for dev DBs).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7a91f44b2c10"
down_revision: Union[str, Sequence[str], None] = "892742dc301d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Detect collisions (same numeric id present in both tables).
    collisions = bind.execute(
        sa.text(
            """
            SELECT ea.id
            FROM email_asks ea
            INNER JOIN chat_asks ca ON ea.id = ca.id
            """
        )
    ).fetchall()
    if collisions:
        collision_ids = [row[0] for row in collisions]
        print(
            f"[7a91f44b2c10] WARNING: {len(collision_ids)} ask IDs exist in "
            f"both email_asks and chat_asks: {collision_ids[:20]}"
            f"{'…' if len(collision_ids) > 20 else ''}. Voice-note attachments "
            f"with these IDs will be classified as email_ask (legacy "
            f"label-resolver behaviour). Re-attach via the dashboard if "
            f"the chat_ask version is the intended target."
        )

    # 2. Drop the old check constraint so we can write transitional values.
    op.execute(
        "ALTER TABLE voice_note_attachments "
        "DROP CONSTRAINT IF EXISTS ck_voice_note_attachments_target_type"
    )

    # 3. Re-classify each ``ask`` row. Email first, then chat. Orphans
    #    are deleted (the source ask was removed but the attachment
    #    survived — stale data we should not preserve under either type).
    op.execute(
        """
        UPDATE voice_note_attachments
           SET target_type = 'email_ask'
         WHERE target_type = 'ask'
           AND target_id IN (SELECT id FROM email_asks)
        """
    )
    op.execute(
        """
        UPDATE voice_note_attachments
           SET target_type = 'chat_ask'
         WHERE target_type = 'ask'
           AND target_id IN (SELECT id FROM chat_asks)
        """
    )
    # Remaining ``ask`` rows reference neither table — drop them.
    op.execute("DELETE FROM voice_note_attachments WHERE target_type = 'ask'")

    # 4. Add the new check constraint excluding the legacy value.
    op.create_check_constraint(
        "ck_voice_note_attachments_target_type",
        "voice_note_attachments",
        "target_type IN ('person','workstream','email_ask','chat_ask')",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE voice_note_attachments "
        "DROP CONSTRAINT IF EXISTS ck_voice_note_attachments_target_type"
    )
    op.execute(
        """
        UPDATE voice_note_attachments
           SET target_type = 'ask'
         WHERE target_type IN ('email_ask', 'chat_ask')
        """
    )
    op.create_check_constraint(
        "ck_voice_note_attachments_target_type",
        "voice_note_attachments",
        "target_type IN ('person','workstream','ask')",
    )
