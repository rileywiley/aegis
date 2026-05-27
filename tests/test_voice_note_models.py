"""Tests for the VoiceNote and VoiceNoteAttachment ORM models.

These tests focus on the ORM-level wiring — column definitions, default
values, relationship configuration, and `__table_args__` constraints.

DB-level cascade and unique-constraint behavior is verified separately
by the migration upgrade/downgrade cycle (see
`alembic/versions/28ff6a6b6e83_add_voice_notes_tables.py`). The Wave 2G
voice-notes repository and Wave 3K endpoint tests will exercise the
real-DB integration paths once those layers land.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import RelationshipProperty

from aegis.db.models import VoiceNote, VoiceNoteAttachment


# ── VoiceNote model ────────────────────────────────────────


class TestVoiceNoteModel:
    def test_tablename(self):
        assert VoiceNote.__tablename__ == "voice_notes"

    def test_required_columns_present(self):
        cols = {c.name for c in VoiceNote.__table__.columns}
        expected = {
            "id",
            "helios_voice_note_id",
            "helios_session_id",
            "started_at",
            "ended_at",
            "duration_seconds",
            "transcript_text",
            "transcript_text_edited",
            "triggered_by",
            "source_device",
            "is_excerpt",
            "excerpt_of_meeting_id",
            "processing_status",
            "embedding",
            "created_at",
        }
        assert expected.issubset(cols)

    def test_helios_voice_note_id_is_unique(self):
        col = VoiceNote.__table__.columns["helios_voice_note_id"]
        assert col.unique is True
        assert col.nullable is False

    def test_required_fields_not_nullable(self):
        not_nullable = {
            "helios_voice_note_id",
            "helios_session_id",
            "started_at",
            "ended_at",
            "duration_seconds",
            "transcript_text",
            "triggered_by",
            "source_device",
            "is_excerpt",
            "processing_status",
        }
        for name in not_nullable:
            col = VoiceNote.__table__.columns[name]
            assert col.nullable is False, f"{name} should be NOT NULL"

    def test_optional_fields_nullable(self):
        for name in ("transcript_text_edited", "excerpt_of_meeting_id", "embedding"):
            assert VoiceNote.__table__.columns[name].nullable is True

    def test_excerpt_fk_set_null_on_meeting_delete(self):
        col = VoiceNote.__table__.columns["excerpt_of_meeting_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "meetings"
        assert fk.ondelete == "SET NULL"

    def test_check_constraints_present(self):
        constraint_names = {
            c.name for c in VoiceNote.__table__.constraints if c.name
        }
        assert "ck_voice_notes_triggered_by" in constraint_names
        assert "ck_voice_notes_processing_status" in constraint_names

    def test_started_at_index(self):
        index_names = {idx.name for idx in VoiceNote.__table__.indexes}
        assert "idx_voice_notes_started_at" in index_names

    def test_default_values(self):
        # ORM-side defaults — used when persisting via a session.
        assert VoiceNote.__table__.columns["source_device"].default.arg == "mac"
        assert VoiceNote.__table__.columns["is_excerpt"].default.arg is False
        assert (
            VoiceNote.__table__.columns["processing_status"].default.arg == "pending"
        )

    def test_attachments_relationship_cascade(self):
        rel = VoiceNote.__mapper__.relationships["attachments"]
        assert isinstance(rel, RelationshipProperty)
        assert rel.cascade.delete_orphan is True
        assert rel.cascade.delete is True
        assert rel.mapper.class_ is VoiceNoteAttachment

    def test_can_instantiate_with_required_fields(self):
        # Pure Python construction — no DB session required. Verifies
        # the Mapped[...] annotations don't choke at instantiation.
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        vn = VoiceNote(
            helios_voice_note_id=1,
            helios_session_id=2,
            started_at=now,
            ended_at=now,
            duration_seconds=12.5,
            transcript_text="hello",
            triggered_by="menu_bar",
        )
        assert vn.helios_voice_note_id == 1
        assert vn.transcript_text == "hello"
        assert vn.triggered_by == "menu_bar"

    async def test_session_add_called(self):
        # Mocked-session smoke test — matches the broader Aegis
        # convention (e.g. test_workstream_detector.py). `add` is sync on
        # AsyncSession; `commit`/`flush` are async.
        session = AsyncMock()
        session.add = MagicMock()
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        vn = VoiceNote(
            helios_voice_note_id=99,
            helios_session_id=1,
            started_at=now,
            ended_at=now,
            duration_seconds=1.0,
            transcript_text="x",
            triggered_by="hotkey",
        )
        session.add(vn)
        await session.commit()
        session.add.assert_called_once_with(vn)
        session.commit.assert_awaited_once()


# ── VoiceNoteAttachment model ──────────────────────────────


class TestVoiceNoteAttachmentModel:
    def test_tablename(self):
        assert VoiceNoteAttachment.__tablename__ == "voice_note_attachments"

    def test_required_columns_present(self):
        cols = {c.name for c in VoiceNoteAttachment.__table__.columns}
        assert cols == {
            "id",
            "voice_note_id",
            "target_type",
            "target_id",
            "is_suggested",
            "confirmed_at",
        }

    def test_voice_note_fk_cascade_on_delete(self):
        col = VoiceNoteAttachment.__table__.columns["voice_note_id"]
        assert col.nullable is False
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "voice_notes"
        assert fk.ondelete == "CASCADE"

    def test_unique_constraint_on_target(self):
        unique_constraints = [
            c
            for c in VoiceNoteAttachment.__table__.constraints
            if isinstance(c, UniqueConstraint)
        ]
        assert len(unique_constraints) == 1
        uc = unique_constraints[0]
        assert uc.name == "uq_voice_note_attachments_target"
        assert {col.name for col in uc.columns} == {
            "voice_note_id",
            "target_type",
            "target_id",
        }

    def test_target_type_check_constraint(self):
        constraint_names = {
            c.name for c in VoiceNoteAttachment.__table__.constraints if c.name
        }
        assert "ck_voice_note_attachments_target_type" in constraint_names

    def test_check_constraint_split_into_email_and_chat_ask(self):
        """Critical #4 (Wave 4) — the legacy ``ask`` value is gone; the
        check constraint accepts ``email_ask`` + ``chat_ask`` instead so
        attachments survive even when the numeric ids collide.
        """
        from sqlalchemy import CheckConstraint

        ck = next(
            c for c in VoiceNoteAttachment.__table__.constraints
            if isinstance(c, CheckConstraint)
            and c.name == "ck_voice_note_attachments_target_type"
        )
        sql = str(ck.sqltext)
        assert "email_ask" in sql
        assert "chat_ask" in sql
        # Legacy bare 'ask' must NOT be in the allowed set anymore.
        assert "'ask'" not in sql.replace("'email_ask'", "").replace(
            "'chat_ask'", ""
        )

    def test_target_index(self):
        index_names = {idx.name for idx in VoiceNoteAttachment.__table__.indexes}
        assert "idx_vna_target" in index_names
        # Verify it covers the (target_type, target_id) tuple
        idx = next(
            i
            for i in VoiceNoteAttachment.__table__.indexes
            if i.name == "idx_vna_target"
        )
        assert isinstance(idx, Index)
        assert [c.name for c in idx.columns] == ["target_type", "target_id"]

    def test_voice_note_back_populates(self):
        rel = VoiceNoteAttachment.__mapper__.relationships["voice_note"]
        assert rel.back_populates == "attachments"
        assert rel.mapper.class_ is VoiceNote

    def test_required_fields_not_nullable(self):
        for name in (
            "voice_note_id",
            "target_type",
            "target_id",
            "is_suggested",
            "confirmed_at",
        ):
            assert (
                VoiceNoteAttachment.__table__.columns[name].nullable is False
            ), f"{name} should be NOT NULL"

    def test_can_instantiate(self):
        att = VoiceNoteAttachment(
            voice_note_id=1,
            target_type="person",
            target_id=42,
            is_suggested=True,
        )
        assert att.target_type == "person"
        assert att.is_suggested is True


# ── Cross-model relationship wiring ────────────────────────


class TestVoiceNoteAttachmentsCollection:
    def test_voice_note_starts_with_empty_attachments(self):
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        vn = VoiceNote(
            helios_voice_note_id=7,
            helios_session_id=3,
            started_at=now,
            ended_at=now,
            duration_seconds=5.0,
            transcript_text="t",
            triggered_by="dashboard",
        )
        # Default is empty list per SQLAlchemy collection semantics
        assert vn.attachments == []

    def test_appending_attachment_sets_back_populates(self):
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        vn = VoiceNote(
            helios_voice_note_id=8,
            helios_session_id=4,
            started_at=now,
            ended_at=now,
            duration_seconds=6.0,
            transcript_text="t2",
            triggered_by="menu_bar",
        )
        att = VoiceNoteAttachment(
            target_type="workstream", target_id=11, is_suggested=False
        )
        vn.attachments.append(att)
        # back_populates wires the inverse side automatically
        assert att.voice_note is vn
        assert vn.attachments == [att]


# ── Smoke test: sanity of allowed values matching the Phase 2 stubs ──


@pytest.mark.parametrize("trigger", ["menu_bar", "hotkey", "dashboard"])
def test_each_valid_trigger_can_be_constructed(trigger):
    """The CHECK constraint values match the Phase 2 stub Pydantic Literal."""
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    vn = VoiceNote(
        helios_voice_note_id=hash(trigger) % 10_000,
        helios_session_id=1,
        started_at=now,
        ended_at=now,
        duration_seconds=1.0,
        transcript_text="x",
        triggered_by=trigger,
    )
    assert vn.triggered_by == trigger


@pytest.mark.parametrize("target", ["person", "workstream", "ask"])
def test_each_valid_target_type_can_be_constructed(target):
    att = VoiceNoteAttachment(
        voice_note_id=1, target_type=target, target_id=1, is_suggested=False
    )
    assert att.target_type == target
