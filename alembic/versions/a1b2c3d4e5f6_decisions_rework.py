"""decisions rework — new columns, updated constraints, ask_type enum change

Revision ID: a1b2c3d4e5f6
Revises: 36444981ccaa
Create Date: 2026-05-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '36444981ccaa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- decisions table: add new columns --
    op.add_column('decisions', sa.Column('outcome', sa.Text(), nullable=True))
    op.add_column('decisions', sa.Column('pending_owner_id', sa.Integer(), sa.ForeignKey('people.id'), nullable=True))
    op.add_column('decisions', sa.Column('source_ask_id', sa.Integer(), sa.ForeignKey('email_asks.id'), nullable=True))
    op.add_column('decisions', sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True))

    # Update status values and add check constraint (constraint didn't exist before)
    op.execute("UPDATE decisions SET status = 'pending' WHERE status = 'open' OR status IS NULL")
    op.create_check_constraint('ck_decisions_status', 'decisions', "status IN ('pending','resolved')")

    # Add indexes
    op.create_index('idx_decisions_pending_owner', 'decisions', ['pending_owner_id'])
    op.create_index('idx_decisions_source_ask', 'decisions', ['source_ask_id'])
    op.create_index('idx_decisions_status', 'decisions', ['status'])

    # -- action_items table: add related_decision_id --
    op.add_column('action_items', sa.Column('related_decision_id', sa.Integer(), sa.ForeignKey('decisions.id'), nullable=True))
    op.create_index('idx_action_items_decision', 'action_items', ['related_decision_id'])

    # -- email_asks: remove 'decision' from ask_type enum --
    op.drop_constraint('ck_email_asks_type', 'email_asks', type_='check')
    # Migrate existing decision asks to 'approval'
    op.execute("UPDATE email_asks SET ask_type = 'approval' WHERE ask_type = 'decision'")
    op.create_check_constraint('ck_email_asks_type', 'email_asks',
        "ask_type IN ('deliverable','follow_up','question','approval','review','info_request')")

    # -- chat_asks: remove 'decision' from ask_type enum --
    op.drop_constraint('ck_chat_asks_type', 'chat_asks', type_='check')
    op.execute("UPDATE chat_asks SET ask_type = 'approval' WHERE ask_type = 'decision'")
    op.create_check_constraint('ck_chat_asks_type', 'chat_asks',
        "ask_type IN ('deliverable','follow_up','question','approval','review','info_request')")


def downgrade() -> None:
    # Revert chat_asks constraint
    op.drop_constraint('ck_chat_asks_type', 'chat_asks', type_='check')
    op.create_check_constraint('ck_chat_asks_type', 'chat_asks',
        "ask_type IN ('deliverable','decision','follow_up','question','approval','review','info_request')")

    # Revert email_asks constraint
    op.drop_constraint('ck_email_asks_type', 'email_asks', type_='check')
    op.create_check_constraint('ck_email_asks_type', 'email_asks',
        "ask_type IN ('deliverable','decision','follow_up','question','approval','review','info_request')")

    # Revert action_items
    op.drop_index('idx_action_items_decision', 'action_items')
    op.drop_column('action_items', 'related_decision_id')

    # Revert decisions
    op.drop_index('idx_decisions_status', 'decisions')
    op.drop_index('idx_decisions_source_ask', 'decisions')
    op.drop_index('idx_decisions_pending_owner', 'decisions')
    op.execute("UPDATE decisions SET status = 'open' WHERE status = 'pending'")
    op.drop_constraint('ck_decisions_status', 'decisions', type_='check')
    op.create_check_constraint('ck_decisions_status', 'decisions', "status IN ('open','resolved')")
    op.drop_column('decisions', 'resolved_at')
    op.drop_column('decisions', 'source_ask_id')
    op.drop_column('decisions', 'pending_owner_id')
    op.drop_column('decisions', 'outcome')
