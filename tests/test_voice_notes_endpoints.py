"""Tests for the Phase 3 voice-note endpoints (Wave 3K).

The endpoints persist via ``VoiceNotesRepository`` and run the entity
resolver against transcripts. Like ``test_meetings_upcoming.py``, we
mock the repository and resolver — no real Postgres in unit tests.

Endpoint contracts:
    POST   /api/voice-notes/preview-attachments
    POST   /api/voice-notes
    GET    /api/voice-notes
    GET    /api/voice-notes/{id}
    PATCH  /api/voice-notes/{id}
    DELETE /api/voice-notes/{id}
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aegis.db.engine import get_session
from aegis.main import app
from aegis.processing.resolver import ResolverMatch


# ── Helpers ────────────────────────────────────────────────


def _make_voice_note(**overrides) -> SimpleNamespace:
    """Build a voice-note-shaped object the route can read."""
    now = datetime.now(timezone.utc)
    defaults = {
        "id": 1,
        "helios_voice_note_id": 100,
        "helios_session_id": 7,
        "started_at": now,
        "ended_at": now + timedelta(seconds=60),
        "duration_seconds": 60.0,
        "transcript_text": "Test transcript.",
        "transcript_text_edited": None,
        "triggered_by": "menu_bar",
        "is_excerpt": False,
        "excerpt_of_meeting_id": None,
        "source_device": "mac",
        "processing_status": "pending",
        "attachments": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_attachment(
    *,
    target_type: str = "person",
    target_id: int = 1,
    is_suggested: bool = False,
    confirmed_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        target_type=target_type,
        target_id=target_id,
        is_suggested=is_suggested,
        confirmed_at=confirmed_at or datetime.now(timezone.utc),
    )


def _make_meeting(
    *,
    id: int = 42,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> SimpleNamespace:
    """Build a meeting-shaped object the excerpt-matcher can read."""
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=id,
        start_time=start_time or (now - timedelta(minutes=10)),
        end_time=end_time or (now + timedelta(minutes=20)),
    )


@pytest.fixture
def client():
    """TestClient with the DB session dependency overridden to a no-op."""
    async def _fake_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = _fake_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


# ═══════════════════════════════════════════════════════════
# /api/voice-notes/preview-attachments
# ═══════════════════════════════════════════════════════════


class TestPreviewAttachments:
    def test_person_match_above_threshold(self, client):
        """Resolver returns a person match → suggested + matches both contain it."""
        body = {
            "transcript_text": "Follow up with Sarah Lin about the budget.",
            "occurred_at": 1714766400.0,
        }
        with patch(
            "aegis.web.routes.voice_notes.resolver_mod.resolve_text",
            new=AsyncMock(return_value=[
                ResolverMatch(
                    target_type="person",
                    target_id=1,
                    label="Sarah Lin",
                    confidence=0.95,
                    span="Sarah Lin",
                ),
            ]),
        ):
            resp = client.post(
                "/api/voice-notes/preview-attachments", json=body
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["suggested_attachments"]) == 1
        assert data["suggested_attachments"][0]["target_type"] == "person"
        assert data["suggested_attachments"][0]["target_id"] == 1
        assert data["suggested_attachments"][0]["confidence"] == 0.95
        assert len(data["matches"]) == 1
        assert data["matches"][0]["span"] == "Sarah Lin"

    def test_no_matches_returns_empty_lists(self, client):
        """Resolver returns nothing → both lists empty."""
        body = {
            "transcript_text": "Random thoughts about nothing in particular.",
            "occurred_at": 1714766400.0,
        }
        with patch(
            "aegis.web.routes.voice_notes.resolver_mod.resolve_text",
            new=AsyncMock(return_value=[]),
        ):
            resp = client.post(
                "/api/voice-notes/preview-attachments", json=body
            )
        assert resp.status_code == 200
        assert resp.json() == {"suggested_attachments": [], "matches": []}

    def test_multi_target_types_all_returned_in_matches(self, client):
        """Person + workstream + email_ask matches all appear in matches list.

        Critical #4 (Wave 4): ask types are split into ``email_ask`` and
        ``chat_ask`` because the numeric ids collide between email_asks
        and chat_asks. The resolver tags each match with the right
        type based on the source.
        """
        body = {
            "transcript_text": "Sarah and the Q3 plan + that approval ask",
            "occurred_at": 1714766400.0,
        }
        all_matches = [
            ResolverMatch("person", 1, "Sarah", 0.9, "Sarah"),
            ResolverMatch("workstream", 5, "Q3 Plan", 0.85, "Q3 Plan"),
            ResolverMatch("email_ask", 9, "approval ask", 0.55, "approval ask"),
        ]
        with patch(
            "aegis.web.routes.voice_notes.resolver_mod.resolve_text",
            new=AsyncMock(return_value=all_matches),
        ):
            resp = client.post(
                "/api/voice-notes/preview-attachments", json=body
            )
        assert resp.status_code == 200
        data = resp.json()
        # All 3 in matches.
        assert len(data["matches"]) == 3
        types = {m["target_type"] for m in data["matches"]}
        assert types == {"person", "workstream", "email_ask"}
        # Only the two ≥0.6 in suggested.
        assert len(data["suggested_attachments"]) == 2
        suggested_ids = {
            (a["target_type"], a["target_id"])
            for a in data["suggested_attachments"]
        }
        assert ("person", 1) in suggested_ids
        assert ("workstream", 5) in suggested_ids
        assert ("email_ask", 9) not in suggested_ids

    def test_confidence_threshold_filters_suggestions(self, client):
        """Threshold=0.6 → 0.5 matches are in matches but not suggestions."""
        body = {
            "transcript_text": "Maybe Sarah is involved",
            "occurred_at": 1714766400.0,
            "confidence_threshold": 0.6,
        }
        with patch(
            "aegis.web.routes.voice_notes.resolver_mod.resolve_text",
            new=AsyncMock(return_value=[
                ResolverMatch("person", 1, "Sarah", 0.5, "Sarah"),
            ]),
        ):
            resp = client.post(
                "/api/voice-notes/preview-attachments", json=body
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["matches"]) == 1
        assert data["suggested_attachments"] == []


# ═══════════════════════════════════════════════════════════
# POST /api/voice-notes
# ═══════════════════════════════════════════════════════════


class TestCreateVoiceNote:
    def test_happy_path_returns_voice_note_id(self, client):
        """Valid body → 200, voice_note_id from repo, repo.create called."""
        body = {
            "helios_voice_note_id": 42,
            "helios_session_id": 7,
            "started_at": 1714766400.0,
            "ended_at": 1714766520.0,
            "duration_seconds": 120.0,
            "transcript_text": "Reminder to ping the platform team.",
            "triggered_by": "menu_bar",
        }
        created_note = _make_voice_note(id=99, helios_voice_note_id=42)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_helios_id = AsyncMock(return_value=None)
            instance.create = AsyncMock(return_value=created_note)
            instance.update_attachments = AsyncMock()

            resp = client.post("/api/voice-notes", json=body)

            assert resp.status_code == 200
            assert resp.json() == {
                "voice_note_id": 99,
                "extraction_status": "queued",
            }
            instance.create.assert_awaited_once()
            # Attachments not in body → update_attachments not called.
            instance.update_attachments.assert_not_called()

    def test_idempotent_on_duplicate_helios_id(self, client):
        """Re-POST with same helios_voice_note_id → existing row returned."""
        body = {
            "helios_voice_note_id": 42,
            "helios_session_id": 7,
            "started_at": 1714766400.0,
            "ended_at": 1714766520.0,
            "duration_seconds": 120.0,
            "transcript_text": "Already saved.",
            "triggered_by": "menu_bar",
        }
        existing = _make_voice_note(id=77, helios_voice_note_id=42)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_helios_id = AsyncMock(return_value=existing)
            instance.create = AsyncMock()

            resp = client.post("/api/voice-notes", json=body)

            assert resp.status_code == 200
            assert resp.json()["voice_note_id"] == 77
            # Critical: create was NOT called on the duplicate path.
            instance.create.assert_not_called()

    def test_excerpt_with_matching_meeting_sets_meeting_id(self, client):
        """is_excerpt=true + overlapping meeting in DB → excerpt_of_meeting_id set."""
        started_dt = datetime(2026, 5, 3, 14, 5, tzinfo=timezone.utc)
        body = {
            "helios_voice_note_id": 5,
            "helios_session_id": 1,
            "started_at": started_dt.timestamp(),
            "ended_at": started_dt.timestamp() + 30,
            "duration_seconds": 30.0,
            "transcript_text": "In a meeting now.",
            "triggered_by": "hotkey",
            "is_excerpt": True,
        }
        meeting = _make_meeting(
            id=42,
            start_time=started_dt - timedelta(minutes=5),
            end_time=started_dt + timedelta(minutes=25),
        )
        created_note = _make_voice_note(id=11, excerpt_of_meeting_id=42)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls, patch(
            "aegis.web.routes.voice_notes.MeetingsRepository"
        ) as meetings_cls:
            repo_cls.return_value.get_by_helios_id = AsyncMock(
                return_value=None
            )
            repo_cls.return_value.create = AsyncMock(return_value=created_note)
            repo_cls.return_value.update_attachments = AsyncMock()
            meetings_cls.return_value.get_in_range = AsyncMock(
                return_value=[meeting]
            )

            resp = client.post("/api/voice-notes", json=body)

            assert resp.status_code == 200
            create_kwargs = repo_cls.return_value.create.await_args.kwargs
            assert create_kwargs["excerpt_of_meeting_id"] == 42
            assert create_kwargs["is_excerpt"] is True

    def test_excerpt_with_no_matching_meeting_sets_none(self, client):
        """is_excerpt=true + no overlapping meeting → excerpt_of_meeting_id=None."""
        body = {
            "helios_voice_note_id": 5,
            "helios_session_id": 1,
            "started_at": 1714766400.0,
            "ended_at": 1714766430.0,
            "duration_seconds": 30.0,
            "transcript_text": "Hot take after the call.",
            "triggered_by": "menu_bar",
            "is_excerpt": True,
        }
        # Meeting that does NOT contain started_at.
        far_future = datetime.now(timezone.utc) + timedelta(days=100)
        non_overlapping = _make_meeting(
            id=99,
            start_time=far_future,
            end_time=far_future + timedelta(minutes=30),
        )
        created_note = _make_voice_note(id=22)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls, patch(
            "aegis.web.routes.voice_notes.MeetingsRepository"
        ) as meetings_cls:
            repo_cls.return_value.get_by_helios_id = AsyncMock(
                return_value=None
            )
            repo_cls.return_value.create = AsyncMock(return_value=created_note)
            repo_cls.return_value.update_attachments = AsyncMock()
            # get_in_range returns nothing in the lookback window.
            meetings_cls.return_value.get_in_range = AsyncMock(return_value=[])

            resp = client.post("/api/voice-notes", json=body)

            assert resp.status_code == 200
            create_kwargs = repo_cls.return_value.create.await_args.kwargs
            assert create_kwargs["excerpt_of_meeting_id"] is None
            # And ensure non_overlapping isn't accidentally used.
            del non_overlapping  # silence flake; just here for clarity

    def test_missing_required_field_rejected(self, client):
        body = {
            "helios_session_id": 1,
            "started_at": 1.0,
            "ended_at": 2.0,
            "duration_seconds": 1.0,
            "transcript_text": "x",
            "triggered_by": "menu_bar",
            # missing helios_voice_note_id
        }
        resp = client.post("/api/voice-notes", json=body)
        assert resp.status_code == 422

    def test_invalid_triggered_by_rejected(self, client):
        body = {
            "helios_voice_note_id": 1,
            "helios_session_id": 1,
            "started_at": 1.0,
            "ended_at": 2.0,
            "duration_seconds": 1.0,
            "transcript_text": "x",
            "triggered_by": "telepathy",  # not in Literal
        }
        resp = client.post("/api/voice-notes", json=body)
        assert resp.status_code == 422

    def test_attachments_propagated_to_repo(self, client):
        """User-confirmed attachments → repo.update_attachments called."""
        body = {
            "helios_voice_note_id": 50,
            "helios_session_id": 1,
            "started_at": 1714766400.0,
            "ended_at": 1714766420.0,
            "duration_seconds": 20.0,
            "transcript_text": "Talk to Sarah",
            "triggered_by": "menu_bar",
            "attachments": [
                {
                    "target_type": "person",
                    "target_id": 7,
                    "is_suggested": True,
                },
                {
                    "target_type": "workstream",
                    "target_id": 3,
                    "is_suggested": False,
                },
            ],
        }
        created_note = _make_voice_note(id=33)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_helios_id = AsyncMock(return_value=None)
            instance.create = AsyncMock(return_value=created_note)
            instance.update_attachments = AsyncMock()

            resp = client.post("/api/voice-notes", json=body)

            assert resp.status_code == 200
            instance.update_attachments.assert_awaited_once()
            args, _ = instance.update_attachments.await_args
            voice_note_id_arg, attachments_arg = args
            assert voice_note_id_arg == 33
            assert len(attachments_arg) == 2
            assert attachments_arg[0]["target_type"] == "person"
            assert attachments_arg[0]["target_id"] == 7
            assert attachments_arg[0]["is_suggested"] is True
            assert attachments_arg[1]["target_type"] == "workstream"


# ═══════════════════════════════════════════════════════════
# GET /api/voice-notes (list)
# ═══════════════════════════════════════════════════════════


class TestListVoiceNotes:
    def test_returns_repo_results(self, client):
        notes = [
            _make_voice_note(id=2, helios_voice_note_id=200),
            _make_voice_note(id=1, helios_voice_note_id=100),
        ]
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.list = AsyncMock(return_value=notes)
            resp = client.get("/api/voice-notes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == 2
        assert data[1]["id"] == 1

    def test_filter_passed_to_repo(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.list = AsyncMock(return_value=[])
            resp = client.get(
                "/api/voice-notes?triggered_by=menu_bar&is_excerpt=true"
            )
            assert resp.status_code == 200
            instance.list.assert_awaited_once()
            kwargs = instance.list.await_args.kwargs
            assert kwargs["triggered_by"] == "menu_bar"
            assert kwargs["is_excerpt"] is True


# ═══════════════════════════════════════════════════════════
# GET /api/voice-notes/{id}
# ═══════════════════════════════════════════════════════════


class TestGetVoiceNote:
    def test_returns_note_when_found(self, client):
        note = _make_voice_note(
            id=7,
            helios_voice_note_id=123,
            attachments=[_make_attachment(target_type="person", target_id=4)],
        )
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=note)
            resp = client.get("/api/voice-notes/7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 7
        assert data["helios_voice_note_id"] == 123
        assert len(data["attachments"]) == 1
        assert data["attachments"][0]["target_type"] == "person"
        assert data["attachments"][0]["target_id"] == 4

    def test_returns_404_when_missing(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            resp = client.get("/api/voice-notes/999")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════
# PATCH /api/voice-notes/{id}
# ═══════════════════════════════════════════════════════════


class TestPatchVoiceNote:
    def test_transcript_edit_calls_repo(self, client):
        note = _make_voice_note(
            id=5, transcript_text_edited="My edit."
        )
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.update_transcript_edit = AsyncMock(return_value=note)
            instance.get_by_id = AsyncMock(return_value=note)
            resp = client.patch(
                "/api/voice-notes/5",
                json={"transcript_text_edited": "My edit."},
            )
            assert resp.status_code == 200
            assert resp.json()["transcript_text_edited"] == "My edit."
            instance.update_transcript_edit.assert_awaited_once_with(
                5, "My edit."
            )

    def test_attachments_calls_repo(self, client):
        note = _make_voice_note(id=6)
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.update_attachments = AsyncMock(return_value=note)
            instance.get_by_id = AsyncMock(return_value=note)
            resp = client.patch(
                "/api/voice-notes/6",
                json={
                    "attachments": [
                        {
                            "target_type": "email_ask",
                            "target_id": 11,
                            "is_suggested": False,
                        }
                    ],
                },
            )
            assert resp.status_code == 200
            instance.update_attachments.assert_awaited_once()
            args, _ = instance.update_attachments.await_args
            assert args[0] == 6
            assert args[1][0]["target_type"] == "email_ask"
            assert args[1][0]["target_id"] == 11

    def test_patch_404_when_missing(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.update_transcript_edit = AsyncMock(return_value=None)
            resp = client.patch(
                "/api/voice-notes/999",
                json={"transcript_text_edited": "edit"},
            )
            assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════
# DELETE /api/voice-notes/{id}
# ═══════════════════════════════════════════════════════════


class TestDeleteVoiceNote:
    def test_delete_returns_204(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.delete = AsyncMock(return_value=True)
            resp = client.delete("/api/voice-notes/3")
        assert resp.status_code == 204
        assert resp.text == ""

    def test_delete_returns_404_when_missing(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.delete = AsyncMock(return_value=False)
            resp = client.delete("/api/voice-notes/999")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════
# New UI-facing endpoints added in Track 6E.3 / 6E.9
# ═══════════════════════════════════════════════════════════


class TestPatchAttachmentsUI:
    def test_add_attachment(self, client):
        """Form PATCH adds a new attachment if not present."""
        note = _make_voice_note(id=5, attachments=[])
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_id = AsyncMock(return_value=note)
            instance.update_attachments = AsyncMock()
            resp = client.patch(
                "/voice-notes/5/attachments",
                data={"add_target_type": "person", "add_target_id": 7},
            )
        assert resp.status_code == 200
        instance.update_attachments.assert_awaited_once()
        args = instance.update_attachments.await_args.args
        assert args[0] == 5
        assert len(args[1]) == 1
        assert args[1][0]["target_type"] == "person"
        assert args[1][0]["target_id"] == 7
        assert args[1][0]["is_suggested"] is False

    def test_remove_attachment(self, client):
        """Form PATCH removes an existing attachment."""
        existing = _make_attachment(target_type="person", target_id=3)
        note = _make_voice_note(id=8, attachments=[existing])
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_id = AsyncMock(return_value=note)
            instance.update_attachments = AsyncMock()
            resp = client.patch(
                "/voice-notes/8/attachments",
                data={"remove_target_type": "person", "remove_target_id": 3},
            )
        assert resp.status_code == 200
        # update_attachments called with empty list
        instance.update_attachments.assert_awaited_once()
        assert instance.update_attachments.await_args.args[1] == []

    def test_invalid_target_type_rejected(self, client):
        note = _make_voice_note(id=8, attachments=[])
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_id = AsyncMock(return_value=note)
            resp = client.patch(
                "/voice-notes/8/attachments",
                data={"add_target_type": "alien", "add_target_id": 1},
            )
        assert resp.status_code == 400

    def test_email_ask_target_type_accepted(self, client):
        """Critical #4 — email_ask is a valid target_type (was rolled in
        from the split of the legacy ``ask`` type)."""
        note = _make_voice_note(id=9, attachments=[])
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_id = AsyncMock(return_value=note)
            instance.update_attachments = AsyncMock()
            resp = client.patch(
                "/voice-notes/9/attachments",
                data={"add_target_type": "email_ask", "add_target_id": 42},
            )
        assert resp.status_code == 200
        args = instance.update_attachments.await_args.args
        assert args[1][0]["target_type"] == "email_ask"

    def test_chat_ask_target_type_accepted(self, client):
        """Critical #4 — chat_ask too."""
        note = _make_voice_note(id=10, attachments=[])
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_id = AsyncMock(return_value=note)
            instance.update_attachments = AsyncMock()
            resp = client.patch(
                "/voice-notes/10/attachments",
                data={"add_target_type": "chat_ask", "add_target_id": 42},
            )
        assert resp.status_code == 200
        args = instance.update_attachments.await_args.args
        assert args[1][0]["target_type"] == "chat_ask"

    def test_add_attachment_idempotent_on_duplicate(self, client):
        """Warning #7 — rapid duplicate clicks should not 5xx; the
        existing attachment set already contains the requested key so
        the repo is called with the same set, and a UNIQUE constraint
        race is swallowed at the route level.
        """
        existing = _make_attachment(target_type="person", target_id=7)
        note = _make_voice_note(id=11, attachments=[existing])
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_by_id = AsyncMock(return_value=note)
            instance.update_attachments = AsyncMock()
            resp = client.patch(
                "/voice-notes/11/attachments",
                data={"add_target_type": "person", "add_target_id": 7},
            )
        assert resp.status_code == 200
        # update_attachments called exactly once with the same set
        # (no duplicate row added).
        instance.update_attachments.assert_awaited_once()
        args = instance.update_attachments.await_args.args
        # No second row appended.
        assert len(args[1]) == 1


class TestStatusPoll:
    def test_status_fragment_for_processing_note(self, client):
        note = _make_voice_note(id=3, processing_status="processing")
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=note)
            resp = client.get("/voice-notes/3/status")
        assert resp.status_code == 200
        body = resp.text
        # Polling continues while processing.
        assert 'hx-trigger="every 2s"' in body
        assert "Re-extracting" in body

    def test_status_fragment_for_completed_note(self, client):
        note = _make_voice_note(id=3, processing_status="completed")
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=note)
            resp = client.get("/voice-notes/3/status")
        assert resp.status_code == 200
        body = resp.text
        # Polling stops once not processing.
        assert 'hx-trigger="none"' in body
        assert "completed" in body


class TestUIDelete:
    def test_delete_via_ui_path(self, client):
        with patch(
            "aegis.web.routes.voice_notes.VoiceNotesRepository"
        ) as repo_cls:
            repo_cls.return_value.delete = AsyncMock(return_value=True)
            resp = client.delete("/voice-notes/3")
        assert resp.status_code == 204


# Unused imports kept silent.
_ = MagicMock
