"""add_voice_notes_tables

Revision ID: 28ff6a6b6e83
Revises: f175f17074af
Create Date: 2026-05-05 16:25:41.869447

Per HELIOS.md §16.12 — adds two new tables for voice notes (Helios capture
subsystem). Voice notes are first-class Aegis entities alongside meetings,
emails, and chat messages. They get extraction, embeddings, and polymorphic
attachments to people / workstreams / asks.

Notes on deviations from §16.12 SQL block:
- Timestamps use TIMESTAMPTZ (project convention) instead of TIMESTAMP.
  All existing tables use timezone-aware timestamps; matching that here.
- Embedding index uses HNSW (project convention) instead of ivfflat.
  All other embedding indexes in 5b73bff49bcf use HNSW with vector_cosine_ops.
- target_type CHECK constraint is added explicitly. The spec listed allowed
  values inline ('person','workstream','ask') but didn't ship a CHECK; we
  add it here to match the style of other polymorphic source_type columns.
- triggered_by CHECK constraint is also added explicitly to match the
  Pydantic Literal in the existing voice-note stub endpoints.
- processing_status CHECK matches the four-state machine used by meetings,
  emails, and chat_messages.
"""
from typing import Sequence, Union

import pgvector.sqlalchemy.vector
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '28ff6a6b6e83'
down_revision: Union[str, Sequence[str], None] = 'f175f17074af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create voice_notes and voice_note_attachments tables."""
    op.create_table(
        'voice_notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('helios_voice_note_id', sa.Integer(), nullable=False),
        sa.Column('helios_session_id', sa.Integer(), nullable=False),
        sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('duration_seconds', sa.Float(), nullable=False),
        sa.Column('transcript_text', sa.Text(), nullable=False),
        sa.Column('transcript_text_edited', sa.Text(), nullable=True),
        sa.Column('triggered_by', sa.String(length=20), nullable=False),
        sa.Column(
            'source_device',
            sa.String(length=20),
            nullable=False,
            server_default='mac',
        ),
        sa.Column(
            'is_excerpt',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column('excerpt_of_meeting_id', sa.Integer(), nullable=True),
        sa.Column(
            'processing_status',
            sa.String(length=20),
            nullable=False,
            server_default='pending',
        ),
        sa.Column(
            'embedding',
            pgvector.sqlalchemy.vector.VECTOR(dim=1536),
            nullable=True,
        ),
        sa.Column(
            'created_at',
            postgresql.TIMESTAMP(timezone=True),
            server_default='now()',
            nullable=False,
        ),
        sa.CheckConstraint(
            "triggered_by IN ('menu_bar','hotkey','dashboard')",
            name='ck_voice_notes_triggered_by',
        ),
        sa.CheckConstraint(
            "processing_status IN ('pending','processing','completed','failed')",
            name='ck_voice_notes_processing_status',
        ),
        sa.ForeignKeyConstraint(
            ['excerpt_of_meeting_id'], ['meetings.id'], ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'helios_voice_note_id', name='uq_voice_notes_helios_voice_note_id',
        ),
    )
    op.create_index(
        'idx_voice_notes_started_at', 'voice_notes', ['started_at'], unique=False,
    )
    # Partial index limited to in-flight states — matches §16.12 spec.
    op.execute(
        "CREATE INDEX idx_voice_notes_processing ON voice_notes (processing_status) "
        "WHERE processing_status IN ('pending', 'processing')"
    )
    # Partial index for excerpts — matches §16.12 spec.
    op.execute(
        "CREATE INDEX idx_voice_notes_excerpt ON voice_notes (excerpt_of_meeting_id) "
        "WHERE excerpt_of_meeting_id IS NOT NULL"
    )
    # HNSW vector index — project convention from 5b73bff49bcf.
    op.execute(
        "CREATE INDEX idx_voice_notes_embedding ON voice_notes "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        'voice_note_attachments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('voice_note_id', sa.Integer(), nullable=False),
        sa.Column('target_type', sa.String(length=20), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('is_suggested', sa.Boolean(), nullable=False),
        sa.Column(
            'confirmed_at',
            postgresql.TIMESTAMP(timezone=True),
            server_default='now()',
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_type IN ('person','workstream','ask')",
            name='ck_voice_note_attachments_target_type',
        ),
        sa.ForeignKeyConstraint(
            ['voice_note_id'], ['voice_notes.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'voice_note_id', 'target_type', 'target_id',
            name='uq_voice_note_attachments_target',
        ),
    )
    op.create_index(
        'idx_vna_target',
        'voice_note_attachments',
        ['target_type', 'target_id'],
        unique=False,
    )


def downgrade() -> None:
    """Drop voice_note_attachments and voice_notes tables."""
    op.drop_index('idx_vna_target', table_name='voice_note_attachments')
    op.drop_table('voice_note_attachments')
    op.execute("DROP INDEX IF EXISTS idx_voice_notes_embedding")
    op.execute("DROP INDEX IF EXISTS idx_voice_notes_excerpt")
    op.execute("DROP INDEX IF EXISTS idx_voice_notes_processing")
    op.drop_index('idx_voice_notes_started_at', table_name='voice_notes')
    op.drop_table('voice_notes')
