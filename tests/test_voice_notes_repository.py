"""Tests for the voice notes repository (Wave 2G).

Uses the AsyncMock pattern from ``tests/test_workstream_detector.py`` —
no real Postgres in unit tests. We assert that the right SQLAlchemy
statements are constructed and that the repository's behavior (flush,
add, etc.) matches the contract.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import Delete, Select

from aegis.db.models import VoiceNote
from aegis.db.voice_notes_repository import VoiceNotesRepository


# ── Helpers ────────────────────────────────────────────────


def _make_session() -> AsyncMock:
    """Build a fresh AsyncMock session with default execute()/flush() set."""
    session = AsyncMock()
    session.add = MagicMock()  # add() is sync in SQLAlchemy
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    return session


def _scalar_one_or_none(value):
    """Build an execute() return value whose .scalar_one_or_none() = value."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalars_all(values):
    """Build an execute() return value whose .scalars().all() = values."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = values
    scalars.unique.return_value = scalars  # unique() returns self
    result.scalars.return_value = scalars
    return result


def _delete_rowcount(rowcount: int):
    """Build an execute() return value with rowcount set (for delete stmts)."""
    result = MagicMock()
    result.rowcount = rowcount
    return result


def _make_voice_note(**overrides) -> MagicMock:
    """Build a MagicMock that looks like a VoiceNote row."""
    now = datetime.now(timezone.utc)
    defaults = {
        "id": 1,
        "helios_voice_note_id": 100,
        "helios_session_id": 7,
        "started_at": now,
        "ended_at": now + timedelta(seconds=60),
        "duration_seconds": 60.0,
        "transcript_text": "test transcript",
        "transcript_text_edited": None,
        "triggered_by": "menu_bar",
        "is_excerpt": False,
        "excerpt_of_meeting_id": None,
        "source_device": "mac",
        "processing_status": "pending",
        "embedding": None,
        "attachments": [],
    }
    defaults.update(overrides)
    vn = MagicMock(spec=VoiceNote)
    for k, v in defaults.items():
        setattr(vn, k, v)
    return vn


# ── create ─────────────────────────────────────────────────


class TestCreate:
    async def test_create_adds_voice_note_and_flushes(self):
        session = _make_session()
        repo = VoiceNotesRepository(session)
        now = datetime.now(timezone.utc)
        vn = await repo.create(
            helios_voice_note_id=42,
            helios_session_id=7,
            started_at=now,
            ended_at=now + timedelta(seconds=120),
            duration_seconds=120.0,
            transcript_text="hello world",
            triggered_by="menu_bar",
        )
        # session.add called with a VoiceNote instance
        assert session.add.call_count == 1
        added = session.add.call_args[0][0]
        assert isinstance(added, VoiceNote)
        assert added.helios_voice_note_id == 42
        assert added.helios_session_id == 7
        assert added.transcript_text == "hello world"
        assert added.triggered_by == "menu_bar"
        assert added.processing_status == "pending"
        assert added.is_excerpt is False
        assert added.source_device == "mac"
        # flush called once
        session.flush.assert_awaited_once()
        # returned the same instance that was added
        assert vn is added

    async def test_create_with_excerpt_populates_meeting_id(self):
        session = _make_session()
        repo = VoiceNotesRepository(session)
        now = datetime.now(timezone.utc)
        vn = await repo.create(
            helios_voice_note_id=43,
            helios_session_id=8,
            started_at=now,
            ended_at=now + timedelta(seconds=30),
            duration_seconds=30.0,
            transcript_text="excerpt body",
            triggered_by="hotkey",
            is_excerpt=True,
            excerpt_of_meeting_id=999,
        )
        assert vn.is_excerpt is True
        assert vn.excerpt_of_meeting_id == 999


# ── read ───────────────────────────────────────────────────


class TestGetById:
    async def test_returns_row_when_found(self):
        session = _make_session()
        target = _make_voice_note(id=5)
        session.execute.return_value = _scalar_one_or_none(target)

        repo = VoiceNotesRepository(session)
        result = await repo.get_by_id(5)

        assert result is target
        # The single execute call should have been a Select on VoiceNote
        # with .where(id == 5) and .options(selectinload(attachments))
        stmt = session.execute.await_args.args[0]
        assert isinstance(stmt, Select)
        compiled = str(stmt)
        assert "voice_notes" in compiled
        # selectinload added an option entry
        assert len(stmt._with_options) == 1

    async def test_returns_none_when_not_found(self):
        session = _make_session()
        session.execute.return_value = _scalar_one_or_none(None)

        repo = VoiceNotesRepository(session)
        result = await repo.get_by_id(999)

        assert result is None

    async def test_eager_loads_attachments(self):
        session = _make_session()
        session.execute.return_value = _scalar_one_or_none(_make_voice_note())

        repo = VoiceNotesRepository(session)
        await repo.get_by_id(1)

        stmt = session.execute.await_args.args[0]
        # Has at least one selectinload option attached.
        assert len(stmt._with_options) >= 1


class TestGetByHeliosId:
    async def test_queries_by_helios_voice_note_id(self):
        session = _make_session()
        target = _make_voice_note(helios_voice_note_id=12345)
        session.execute.return_value = _scalar_one_or_none(target)

        repo = VoiceNotesRepository(session)
        result = await repo.get_by_helios_id(12345)

        assert result is target
        stmt = session.execute.await_args.args[0]
        assert isinstance(stmt, Select)
        # The where clause should reference helios_voice_note_id.
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "helios_voice_note_id" in compiled

    async def test_returns_none_when_not_found(self):
        session = _make_session()
        session.execute.return_value = _scalar_one_or_none(None)

        repo = VoiceNotesRepository(session)
        result = await repo.get_by_helios_id(404)
        assert result is None


class TestList:
    async def test_no_filters_returns_all_with_default_limit(self):
        session = _make_session()
        rows = [_make_voice_note(id=i) for i in range(3)]
        session.execute.return_value = _scalars_all(rows)

        repo = VoiceNotesRepository(session)
        result = await repo.list()

        assert result == rows
        stmt = session.execute.await_args.args[0]
        assert isinstance(stmt, Select)
        # Default limit/offset.
        assert stmt._limit == 50
        assert stmt._offset == 0

    async def test_filters_by_triggered_by(self):
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list(triggered_by="menu_bar")

        stmt = session.execute.await_args.args[0]
        # The where clauses should include triggered_by filter.
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "triggered_by" in compiled
        assert "menu_bar" in compiled

    async def test_filters_by_processing_status(self):
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list(processing_status="pending")

        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "processing_status" in compiled
        assert "pending" in compiled

    async def test_filters_by_is_excerpt(self):
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list(is_excerpt=True)

        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "is_excerpt" in compiled

    async def test_respects_limit_and_offset(self):
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list(limit=10, offset=20)

        stmt = session.execute.await_args.args[0]
        assert stmt._limit == 10
        assert stmt._offset == 20


class TestListInRange:
    async def test_filters_by_started_at_range(self):
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 31, tzinfo=timezone.utc)

        repo = VoiceNotesRepository(session)
        await repo.list_in_range(start, end)

        stmt = session.execute.await_args.args[0]
        assert isinstance(stmt, Select)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # Both start and end should appear in the WHERE clause.
        assert "started_at" in compiled


class TestListForTarget:
    async def test_list_for_person_joins_through_attachments(self):
        session = _make_session()
        rows = [_make_voice_note(id=1)]
        session.execute.return_value = _scalars_all(rows)

        repo = VoiceNotesRepository(session)
        result = await repo.list_for_person(42)

        assert result == rows
        stmt = session.execute.await_args.args[0]
        assert isinstance(stmt, Select)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "voice_note_attachments" in compiled
        assert "person" in compiled
        # target_id 42 should appear as a literal in the WHERE clause.
        assert "42" in compiled

    async def test_list_for_workstream_uses_workstream_target_type(self):
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list_for_workstream(7)

        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "workstream" in compiled
        assert "7" in compiled

    async def test_list_for_ask_email_uses_email_ask_target_type(self):
        """Critical #4 (Wave 4): ``list_for_ask`` requires ``source=`` to
        disambiguate email_asks vs chat_asks (the numeric ids collide).
        Asking for the email side hits ``target_type='email_ask'``.
        """
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list_for_ask(99, source="email")

        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "email_ask" in compiled
        assert "99" in compiled

    async def test_list_for_ask_chat_uses_chat_ask_target_type(self):
        """Critical #4 (Wave 4): the chat side hits 'chat_ask'."""
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list_for_ask(99, source="chat")

        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "chat_ask" in compiled
        assert "99" in compiled

    async def test_list_for_target_respects_limit(self):
        session = _make_session()
        session.execute.return_value = _scalars_all([])

        repo = VoiceNotesRepository(session)
        await repo.list_for_target("person", 1, limit=5)

        stmt = session.execute.await_args.args[0]
        assert stmt._limit == 5


# ── update ─────────────────────────────────────────────────


class TestUpdateTranscriptEdit:
    async def test_updates_field_when_note_exists(self):
        session = _make_session()
        vn = _make_voice_note(id=1, transcript_text_edited=None)
        session.execute.return_value = _scalar_one_or_none(vn)

        repo = VoiceNotesRepository(session)
        result = await repo.update_transcript_edit(1, "edited body")

        assert result is vn
        assert vn.transcript_text_edited == "edited body"
        session.flush.assert_awaited()

    async def test_returns_none_when_note_does_not_exist(self):
        session = _make_session()
        session.execute.return_value = _scalar_one_or_none(None)

        repo = VoiceNotesRepository(session)
        result = await repo.update_transcript_edit(999, "edited body")

        assert result is None


class TestUpdateAttachments:
    async def test_deletes_existing_then_inserts_new(self):
        session = _make_session()
        vn = _make_voice_note(id=1)
        # First execute() = get_by_id (returns the note)
        # Second execute() = the DELETE statement
        # Third execute() = get_by_id again (re-fetch with attachments)
        session.execute.side_effect = [
            _scalar_one_or_none(vn),
            _delete_rowcount(0),
            _scalar_one_or_none(vn),
        ]

        repo = VoiceNotesRepository(session)
        result = await repo.update_attachments(
            1,
            [
                {"target_type": "person", "target_id": 10, "is_suggested": True},
                {"target_type": "workstream", "target_id": 5},
            ],
        )

        assert result is vn
        # Should have executed exactly: get + delete + get.
        assert session.execute.await_count == 3
        # The middle execute should be a Delete on VoiceNoteAttachment.
        delete_stmt = session.execute.await_args_list[1].args[0]
        assert isinstance(delete_stmt, Delete)
        # session.add called twice (one per new attachment).
        assert session.add.call_count == 2
        added_targets = {
            (call.args[0].target_type, call.args[0].target_id)
            for call in session.add.call_args_list
        }
        assert added_targets == {("person", 10), ("workstream", 5)}
        # is_suggested defaults to False when omitted.
        for call in session.add.call_args_list:
            obj = call.args[0]
            if obj.target_type == "workstream":
                assert obj.is_suggested is False
            elif obj.target_type == "person":
                assert obj.is_suggested is True

    async def test_returns_none_when_note_does_not_exist(self):
        session = _make_session()
        session.execute.return_value = _scalar_one_or_none(None)

        repo = VoiceNotesRepository(session)
        result = await repo.update_attachments(
            999, [{"target_type": "person", "target_id": 1}]
        )

        assert result is None
        # No DELETE, no add — only the initial get_by_id execute.
        assert session.execute.await_count == 1
        session.add.assert_not_called()

    async def test_idempotent_on_identical_input(self):
        """Calling twice with the same attachments should not raise.

        The delete-then-insert pattern means the unique constraint never
        fires on the second call — existing rows are removed first.
        """
        session = _make_session()
        vn = _make_voice_note(id=1)
        # First call: get + delete + get
        # Second call: same pattern
        session.execute.side_effect = [
            _scalar_one_or_none(vn),
            _delete_rowcount(0),
            _scalar_one_or_none(vn),
            _scalar_one_or_none(vn),
            _delete_rowcount(1),
            _scalar_one_or_none(vn),
        ]

        repo = VoiceNotesRepository(session)
        attachments = [{"target_type": "person", "target_id": 10}]
        await repo.update_attachments(1, attachments)
        await repo.update_attachments(1, attachments)

        # Two delete statements, two adds (one per call).
        assert session.add.call_count == 2

    async def test_passes_through_confirmed_at_when_provided(self):
        session = _make_session()
        vn = _make_voice_note(id=1)
        session.execute.side_effect = [
            _scalar_one_or_none(vn),
            _delete_rowcount(0),
            _scalar_one_or_none(vn),
        ]
        when = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        repo = VoiceNotesRepository(session)
        await repo.update_attachments(
            1,
            [
                {
                    "target_type": "person",
                    "target_id": 10,
                    "is_suggested": False,
                    "confirmed_at": when,
                }
            ],
        )

        added = session.add.call_args[0][0]
        assert added.confirmed_at == when


class TestMarkProcessingStatus:
    async def test_updates_field(self):
        session = _make_session()
        vn = _make_voice_note(id=1, processing_status="pending")
        session.execute.return_value = _scalar_one_or_none(vn)

        repo = VoiceNotesRepository(session)
        await repo.mark_processing_status(1, "processing")

        assert vn.processing_status == "processing"
        session.flush.assert_awaited()

    async def test_noop_when_note_does_not_exist(self):
        session = _make_session()
        session.execute.return_value = _scalar_one_or_none(None)

        repo = VoiceNotesRepository(session)
        # Should not raise.
        await repo.mark_processing_status(999, "completed")


class TestSetEmbedding:
    async def test_updates_embedding_field(self):
        session = _make_session()
        vn = _make_voice_note(id=1, embedding=None)
        session.execute.return_value = _scalar_one_or_none(vn)

        repo = VoiceNotesRepository(session)
        embedding = [0.1] * 1536
        await repo.set_embedding(1, embedding)

        assert vn.embedding == embedding
        session.flush.assert_awaited()

    async def test_noop_when_note_does_not_exist(self):
        session = _make_session()
        session.execute.return_value = _scalar_one_or_none(None)

        repo = VoiceNotesRepository(session)
        # Should not raise.
        await repo.set_embedding(999, [0.0] * 1536)


# ── delete ─────────────────────────────────────────────────


class TestDelete:
    async def test_returns_true_when_row_deleted(self):
        session = _make_session()
        session.execute.return_value = _delete_rowcount(1)

        repo = VoiceNotesRepository(session)
        result = await repo.delete(1)

        assert result is True
        # Statement should be a Delete on VoiceNote.
        stmt = session.execute.await_args.args[0]
        assert isinstance(stmt, Delete)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "voice_notes" in compiled

    async def test_returns_false_when_row_did_not_exist(self):
        session = _make_session()
        session.execute.return_value = _delete_rowcount(0)

        repo = VoiceNotesRepository(session)
        result = await repo.delete(999)

        assert result is False

    async def test_handles_none_rowcount(self):
        """Some drivers return None instead of 0 for rowcount on a no-op."""
        session = _make_session()
        result_obj = MagicMock()
        result_obj.rowcount = None
        session.execute.return_value = result_obj

        repo = VoiceNotesRepository(session)
        assert await repo.delete(1) is False

    async def test_cascade_to_attachments_documented(self):
        """Real cascade is enforced by the FK ON DELETE CASCADE clause in
        Wave 1D's migration (28ff6a6b6e83). The repository simply issues a
        DELETE on voice_notes; the database removes the matching
        voice_note_attachments rows automatically. This test asserts the
        repository does NOT issue a separate DELETE for attachments —
        that's the DB's job.
        """
        session = _make_session()
        session.execute.return_value = _delete_rowcount(1)

        repo = VoiceNotesRepository(session)
        await repo.delete(1)

        # Exactly one execute() call — only the voice_notes DELETE.
        assert session.execute.await_count == 1
        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "voice_notes" in compiled
        # Did NOT explicitly delete from voice_note_attachments.
        assert "voice_note_attachments" not in compiled


# ── Smoke test: import path is stable for downstream waves ─


def test_repository_module_exposes_target_type_alias():
    from aegis.db.voice_notes_repository import TargetType  # noqa: F401
    # Just importing is the assertion.


def test_repository_class_constructor_accepts_session():
    session = _make_session()
    repo = VoiceNotesRepository(session)
    assert repo._session is session
