"""Tests for voice-note sections on workstream / ask profile pages.

We use the FastAPI TestClient with a no-op DB session override and
mock the voice notes repository + sibling queries the routes call.

We don't have a dedicated /people/{id} detail page yet — that gap is
called out in the deliverable report. Tests here cover workstream
and ask details and the new /voice-notes UI page.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aegis.db.engine import get_session
from aegis.main import app


def _vn(id: int = 1, text: str = "Hello world", attachments=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=id,
        helios_voice_note_id=100 + id,
        helios_session_id=1,
        started_at=now,
        ended_at=now + timedelta(seconds=30),
        duration_seconds=30.0,
        transcript_text=text,
        transcript_text_edited=None,
        triggered_by="menu_bar",
        is_excerpt=False,
        excerpt_of_meeting_id=None,
        source_device="mac",
        processing_status="completed",
        attachments=attachments or [],
    )


@pytest.fixture
def client():
    async def _fake_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = _fake_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


# ─────────────────────────────────────────────────────────────
# /voice-notes list page
# ─────────────────────────────────────────────────────────────


class TestVoiceNotesListPage:
    def test_list_page_renders_with_notes(self, client):
        notes = [_vn(id=1, text="First note"), _vn(id=2, text="Second")]

        # Patch all SQL helpers used by the list endpoint.
        with patch("aegis.web.routes.voice_notes.select") as sel:
            # We can't easily patch SQLAlchemy select chains, so just patch
            # the session's execute to return scripted results.
            pass

        # Replace dependency-injected session with one whose execute
        # returns scripted results matching the route's needs.
        scripted = MagicMock()

        async def fake_execute(stmt, *args, **kwargs):
            # The route runs:
            #   1) a count query  → return scalar 2
            #   2) a list query   → return scalars().unique().all() → notes
            # We distinguish by checking the order of calls.
            if not hasattr(fake_execute, "calls"):
                fake_execute.calls = 0
            fake_execute.calls += 1
            r = MagicMock()
            if fake_execute.calls == 1:
                # count
                r.scalar_one = MagicMock(return_value=len(notes))
            else:
                u = MagicMock()
                u.all = MagicMock(return_value=notes)
                s = MagicMock()
                s.unique = MagicMock(return_value=u)
                r.scalars = MagicMock(return_value=s)
            return r

        scripted.execute = AsyncMock(side_effect=fake_execute)
        scripted.commit = AsyncMock()

        async def fake_session():
            yield scripted

        app.dependency_overrides[get_session] = fake_session
        try:
            resp = client.get("/voice-notes")
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides[get_session] = lambda: (yield AsyncMock())

        assert resp.status_code == 200
        body = resp.text
        assert "First note" in body
        assert "Second" in body
        assert "Voice Notes" in body

    def test_list_page_empty_state(self, client):
        scripted = MagicMock()

        async def fake_execute(stmt, *args, **kwargs):
            if not hasattr(fake_execute, "calls"):
                fake_execute.calls = 0
            fake_execute.calls += 1
            r = MagicMock()
            if fake_execute.calls == 1:
                r.scalar_one = MagicMock(return_value=0)
            else:
                u = MagicMock()
                u.all = MagicMock(return_value=[])
                s = MagicMock()
                s.unique = MagicMock(return_value=u)
                r.scalars = MagicMock(return_value=s)
            return r

        scripted.execute = AsyncMock(side_effect=fake_execute)

        async def fake_session():
            yield scripted

        app.dependency_overrides[get_session] = fake_session
        try:
            resp = client.get("/voice-notes")
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides[get_session] = lambda: (yield AsyncMock())

        assert resp.status_code == 200
        # Empty state copy
        assert "No voice notes match these filters" in resp.text


# ─────────────────────────────────────────────────────────────
# /voice-notes/{id} detail page
# ─────────────────────────────────────────────────────────────


class TestVoiceNoteDetailPage:
    def test_detail_page_renders(self, client):
        note = _vn(id=5, text="My transcript.")
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=note)

            # Linked action items query — return empty
            scripted = MagicMock()
            async def fake_execute(*args, **kwargs):
                r = MagicMock()
                s = MagicMock()
                s.all = MagicMock(return_value=[])
                r.scalars = MagicMock(return_value=s)
                # _resolve_attachment_labels lookups also use execute()
                r.all = MagicMock(return_value=[])
                return r
            scripted.execute = AsyncMock(side_effect=fake_execute)

            async def fake_session():
                yield scripted

            app.dependency_overrides[get_session] = fake_session
            try:
                resp = client.get("/voice-notes/5")
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides[get_session] = lambda: (yield AsyncMock())

        assert resp.status_code == 200
        body = resp.text
        assert "My transcript" in body
        assert "Re-extract" in body
        # Transcript editor present
        assert 'name="transcript_text_edited"' in body

    def test_detail_page_404_missing(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            resp = client.get("/voice-notes/999")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────
# Re-extract endpoint (Track 6E.9)
# ─────────────────────────────────────────────────────────────


class TestReExtract:
    def test_re_extract_marks_processing_and_schedules(self, client):
        note = _vn(id=7)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls, patch(
            "aegis.web.routes.voice_notes._schedule_reextract"
        ) as scheduler:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=note)
            repo_cls.return_value.mark_processing_status = AsyncMock()

            # Also override session so DELETE statement runs cleanly.
            scripted = MagicMock()
            scripted.execute = AsyncMock()
            scripted.commit = AsyncMock()

            async def fake_session():
                yield scripted

            app.dependency_overrides[get_session] = fake_session
            try:
                resp = client.post("/voice-notes/7/re-extract")
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides[get_session] = lambda: (yield AsyncMock())

        assert resp.status_code == 200
        assert "Re-extracting" in resp.text
        repo_cls.return_value.mark_processing_status.assert_awaited_once_with(7, "processing")
        scheduler.assert_called_once_with(7)

    def test_re_extract_404_when_missing(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            resp = client.post("/voice-notes/999/re-extract")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────
# /voice-notes/{id}/transcript (inline edit triggers re-extract)
# ─────────────────────────────────────────────────────────────


class TestTranscriptEdit:
    def test_inline_edit_triggers_reextract(self, client):
        """Form POST → repo.update_transcript_edit → schedule background re-extract."""
        note = _vn(id=11)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls, patch(
            "aegis.web.routes.voice_notes._schedule_reextract"
        ) as scheduler:
            instance = repo_cls.return_value
            instance.update_transcript_edit = AsyncMock(return_value=note)
            instance.mark_processing_status = AsyncMock()

            resp = client.patch(
                "/voice-notes/11/transcript",
                data={"transcript_text_edited": "Updated text."},
            )

        assert resp.status_code == 200
        instance.update_transcript_edit.assert_awaited_once_with(11, "Updated text.")
        instance.mark_processing_status.assert_awaited_once_with(11, "processing")
        scheduler.assert_called_once_with(11)
        assert "Re-extracting" in resp.text

    def test_inline_edit_missing_field_returns_400(self, client):
        resp = client.patch("/voice-notes/11/transcript", data={})
        assert resp.status_code == 400

    def test_inline_edit_404_for_missing(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.update_transcript_edit = AsyncMock(return_value=None)
            resp = client.patch(
                "/voice-notes/999/transcript",
                data={"transcript_text_edited": "x"},
            )
        assert resp.status_code == 404
