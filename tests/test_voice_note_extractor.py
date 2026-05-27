"""Tests for the voice note extractor (Wave 4L).

Mocks Anthropic, the resolver, the embedder, and the workstream
detector. Focuses on:

- prompt construction + JSON parsing
- triage="noise" short-circuit
- empty transcript handling
- user-confirmed attachment merge (the canary test for Wave 4L)
- failure path marks processing_status='failed'
- already-deleted note no-ops cleanly
- pipeline scheduler picks up pending voice notes
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.processing import voice_note_extractor as vne


# ── Helpers ──────────────────────────────────────────────────


def _make_haiku_response(content_text: str, input_tokens: int = 100, output_tokens: int = 50):
    block = MagicMock()
    block.text = content_text
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _make_voice_note(
    *,
    voice_note_id: int = 1,
    transcript: str = "Note to self, follow up with Sarah about Q2 budget.",
    transcript_edited: str | None = None,
    attachments: list | None = None,
    processing_status: str = "pending",
):
    """Build a MagicMock that walks like a VoiceNote ORM row."""
    now = datetime.now(timezone.utc)
    vn = MagicMock()
    vn.id = voice_note_id
    vn.helios_voice_note_id = 100 + voice_note_id
    vn.helios_session_id = 7
    vn.started_at = now
    vn.ended_at = now + timedelta(seconds=30)
    vn.duration_seconds = 30.0
    vn.transcript_text = transcript
    vn.transcript_text_edited = transcript_edited
    vn.triggered_by = "menu_bar"
    vn.is_excerpt = False
    vn.excerpt_of_meeting_id = None
    vn.source_device = "mac"
    vn.processing_status = processing_status
    vn.embedding = None
    vn.attachments = attachments or []
    return vn


def _make_attachment(*, target_type: str, target_id: int, is_suggested: bool):
    a = MagicMock()
    a.target_type = target_type
    a.target_id = target_id
    a.is_suggested = is_suggested
    a.confirmed_at = datetime.now(timezone.utc) if not is_suggested else None
    return a


CANNED_EXTRACTION = {
    "triage_class": "substantive",
    "summary": "User wants to follow up with Sarah about the Q2 budget.",
    "action_items": [
        {
            "description": "Follow up with Sarah about Q2 budget",
            "deadline": None,
            "related_person": "Sarah",
        }
    ],
    "mentioned_people": ["Sarah"],
    "mentioned_workstreams": ["Q2 Budget"],
    "topics": ["Q2 budget", "follow up"],
}


# ── extract_voice_note ──────────────────────────────────────


async def test_extract_voice_note_parses_substantive():
    """Haiku JSON → VoiceNoteExtraction; substantive triage returns the parsed model."""
    mock_resp = _make_haiku_response(json.dumps(CANNED_EXTRACTION))
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    vn = _make_voice_note()

    with patch.object(vne, "AsyncAnthropic", return_value=mock_client):
        with patch.object(vne, "get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "test-key"
            result = await vne.extract_voice_note(mock_session, vn)

    assert result is not None
    assert result.triage_class == "substantive"
    assert len(result.action_items) == 1
    assert result.action_items[0].related_person == "Sarah"
    assert "Sarah" in result.mentioned_people

    # Verify the prompt actually contained the transcript.
    call_args = mock_client.messages.create.call_args
    prompt_sent = call_args.kwargs["messages"][0]["content"]
    assert "Q2 budget" in prompt_sent
    assert "Note to self" in prompt_sent


async def test_extract_voice_note_noise_returns_none():
    """triage_class='noise' → caller should skip extraction. Returns None."""
    noise_result = {
        "triage_class": "noise",
        "summary": "",
        "action_items": [],
        "mentioned_people": [],
        "mentioned_workstreams": [],
        "topics": [],
    }
    mock_resp = _make_haiku_response(json.dumps(noise_result))
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    vn = _make_voice_note(transcript="test test one two three")
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    with patch.object(vne, "AsyncAnthropic", return_value=mock_client):
        with patch.object(vne, "get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "test-key"
            result = await vne.extract_voice_note(mock_session, vn)

    assert result is None


async def test_extract_voice_note_empty_transcript_returns_none():
    """Empty transcript → triage as noise without burning a Haiku call."""
    vn = _make_voice_note(transcript="")
    mock_session = AsyncMock()

    # The Haiku client should NOT be called at all.
    with patch.object(vne, "AsyncAnthropic") as mock_client_cls:
        result = await vne.extract_voice_note(mock_session, vn)

    assert result is None
    mock_client_cls.assert_not_called()


async def test_extract_voice_note_strips_markdown_code_fence():
    """Haiku output wrapped in ```json fences should still parse."""
    wrapped = f"```json\n{json.dumps(CANNED_EXTRACTION)}\n```"
    mock_resp = _make_haiku_response(wrapped)
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    vn = _make_voice_note()
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    with patch.object(vne, "AsyncAnthropic", return_value=mock_client):
        with patch.object(vne, "get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "test-key"
            result = await vne.extract_voice_note(mock_session, vn)

    assert result is not None
    assert result.triage_class == "substantive"


async def test_extract_voice_note_prefers_edited_transcript():
    """transcript_text_edited overrides transcript_text in the prompt."""
    mock_resp = _make_haiku_response(json.dumps(CANNED_EXTRACTION))
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    vn = _make_voice_note(
        transcript="ORIGINAL",
        transcript_edited="EDITED VERSION about Q2 budget",
    )
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    with patch.object(vne, "AsyncAnthropic", return_value=mock_client):
        with patch.object(vne, "get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "test-key"
            await vne.extract_voice_note(mock_session, vn)

    prompt_sent = mock_client.messages.create.call_args.kwargs["messages"][0][
        "content"
    ]
    assert "EDITED VERSION" in prompt_sent
    assert "ORIGINAL" not in prompt_sent


# ── store_voice_note_extraction ─────────────────────────────


async def test_store_merges_user_confirmed_attachments():
    """CRITICAL: user-confirmed attachments must survive re-extraction.

    Setup: voice note has one user-confirmed attachment (person 42) AND
    one prior suggested attachment (workstream 11). Resolver returns:
    (a) person 42 again — must not duplicate or flip is_suggested
    (b) workstream 99 — new suggestion to add

    Expected merged set:
    - person 42 with is_suggested=False (preserved from user)
    - workstream 99 with is_suggested=True (new)
    - workstream 11 from resolver only if returned; in this test the
      resolver only returns 42 + 99, so the prior suggestion is dropped
    """
    user_confirmed = _make_attachment(
        target_type="person", target_id=42, is_suggested=False
    )
    prior_suggestion = _make_attachment(
        target_type="workstream", target_id=11, is_suggested=True
    )
    vn = _make_voice_note(
        attachments=[user_confirmed, prior_suggestion],
        transcript="Follow up with person 42 about workstream 99",
    )

    # Build a fake resolver match list: re-suggests person 42 + adds ws 99.
    from aegis.processing.resolver import ResolverMatch

    fake_matches = [
        ResolverMatch(
            target_type="person",
            target_id=42,
            label="Sarah",
            confidence=0.9,
            span="Sarah",
        ),
        ResolverMatch(
            target_type="workstream",
            target_id=99,
            label="Q2 Budget",
            confidence=0.8,
            span="Q2 budget",
        ),
    ]

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_repo = AsyncMock()
    mock_repo.update_attachments = AsyncMock()
    mock_repo.set_embedding = AsyncMock()

    extraction = vne.VoiceNoteExtraction(**CANNED_EXTRACTION)

    with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
        with patch.object(
            vne.resolver_mod, "resolve_text", AsyncMock(return_value=fake_matches)
        ):
            with patch.object(vne, "embed_text", AsyncMock(return_value=[0.0] * 1536)):
                with patch.object(
                    vne, "_assign_voice_note_to_workstreams", AsyncMock(return_value=0)
                ):
                    await vne.store_voice_note_extraction(mock_session, vn, extraction)

    # Inspect the call to update_attachments — this is the merge canary.
    assert mock_repo.update_attachments.await_count == 1
    args, kwargs = mock_repo.update_attachments.await_args
    voice_note_id_arg, merged_arg = args
    assert voice_note_id_arg == vn.id

    # Lookup the entries by target.
    by_target = {(m["target_type"], m["target_id"]): m for m in merged_arg}

    # User-confirmed person survives, with is_suggested=False intact.
    assert ("person", 42) in by_target
    assert by_target[("person", 42)]["is_suggested"] is False
    # The new workstream 99 is added as a suggestion.
    assert ("workstream", 99) in by_target
    assert by_target[("workstream", 99)]["is_suggested"] is True
    # The prior workstream-11 suggestion is dropped because resolver
    # didn't surface it again — that's correct behavior; user-confirmed
    # rows stick, suggestions are recomputed.
    assert ("workstream", 11) not in by_target


async def test_store_creates_action_items_with_resolved_assignee():
    """Action items get assignee_id from resolver person matches."""
    vn = _make_voice_note()
    from aegis.processing.resolver import ResolverMatch

    fake_matches = [
        ResolverMatch(
            target_type="person",
            target_id=42,
            label="Sarah Lin",
            confidence=0.9,
            span="Sarah",
        )
    ]

    mock_session = AsyncMock()
    added: list = []
    mock_session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    mock_session.flush = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_repo = AsyncMock()
    mock_repo.update_attachments = AsyncMock()
    mock_repo.set_embedding = AsyncMock()

    extraction = vne.VoiceNoteExtraction(**CANNED_EXTRACTION)

    with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
        with patch.object(
            vne.resolver_mod, "resolve_text", AsyncMock(return_value=fake_matches)
        ):
            with patch.object(vne, "embed_text", AsyncMock(return_value=[0.0] * 1536)):
                with patch.object(
                    vne, "_assign_voice_note_to_workstreams", AsyncMock(return_value=0)
                ):
                    await vne.store_voice_note_extraction(mock_session, vn, extraction)

    # An ActionItem row was added with assignee_id=42 and source_voice_note_id=1.
    from aegis.db.models import ActionItem

    action_items = [obj for obj in added if isinstance(obj, ActionItem)]
    assert len(action_items) == 1
    ai = action_items[0]
    assert ai.assignee_id == 42
    assert ai.source_voice_note_id == vn.id
    assert "Sarah" in ai.description


async def test_store_calls_set_embedding_with_1536_vector():
    """The transcript embedding must hit set_embedding."""
    vn = _make_voice_note()
    fake_embedding = [0.1] * 1536

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_repo = AsyncMock()
    mock_repo.update_attachments = AsyncMock()
    mock_repo.set_embedding = AsyncMock()

    extraction = vne.VoiceNoteExtraction(**CANNED_EXTRACTION)

    with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
        with patch.object(
            vne.resolver_mod, "resolve_text", AsyncMock(return_value=[])
        ):
            with patch.object(
                vne, "embed_text", AsyncMock(return_value=fake_embedding)
            ):
                with patch.object(
                    vne, "_assign_voice_note_to_workstreams", AsyncMock(return_value=0)
                ):
                    await vne.store_voice_note_extraction(mock_session, vn, extraction)

    mock_repo.set_embedding.assert_awaited()
    args, _ = mock_repo.set_embedding.await_args
    assert args[0] == vn.id
    assert len(args[1]) == 1536


async def test_store_handles_no_recognizable_entities():
    """Voice note with action item but no resolvable people still creates
    an action item (assignee_id=None means the user is the assignee)."""
    vn = _make_voice_note(transcript="Remind me to clean up the build script")

    extraction_dict = {
        "triage_class": "substantive",
        "summary": "Clean up the build script.",
        "action_items": [
            {
                "description": "Clean up the build script",
                "deadline": None,
                "related_person": None,
            }
        ],
        "mentioned_people": [],
        "mentioned_workstreams": [],
        "topics": ["build script"],
    }
    extraction = vne.VoiceNoteExtraction(**extraction_dict)

    mock_session = AsyncMock()
    added: list = []
    mock_session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    mock_session.flush = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_repo = AsyncMock()
    mock_repo.update_attachments = AsyncMock()
    mock_repo.set_embedding = AsyncMock()

    with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
        with patch.object(
            vne.resolver_mod, "resolve_text", AsyncMock(return_value=[])
        ):
            with patch.object(vne, "embed_text", AsyncMock(return_value=[0.0] * 1536)):
                with patch.object(
                    vne, "_assign_voice_note_to_workstreams", AsyncMock(return_value=0)
                ):
                    await vne.store_voice_note_extraction(mock_session, vn, extraction)

    from aegis.db.models import ActionItem

    action_items = [o for o in added if isinstance(o, ActionItem)]
    assert len(action_items) == 1
    assert action_items[0].assignee_id is None  # user is the implicit assignee


# ── process_voice_note (full pipeline) ──────────────────────


async def test_process_voice_note_already_deleted_returns_false():
    """get_by_id returns None → process_voice_note returns False, no error."""
    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=None)

    with patch.object(vne, "async_session_factory") as mock_factory:
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
            result = await vne.process_voice_note(999)

    assert result is False


async def test_process_voice_note_failure_marks_failed_and_raises():
    """If extract_voice_note raises, processing_status becomes 'failed'."""
    vn = _make_voice_note()

    sessions: list[AsyncMock] = []

    def make_session():
        s = AsyncMock()
        s.commit = AsyncMock()
        s.rollback = AsyncMock()
        sessions.append(s)
        return s

    factory_mock = MagicMock()

    def factory_call(*_args, **_kwargs):
        cm = MagicMock()
        s = make_session()
        cm.__aenter__ = AsyncMock(return_value=s)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    factory_mock.side_effect = factory_call

    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=vn)
    mock_repo.mark_processing_status = AsyncMock()

    boom = RuntimeError("haiku exploded")

    with patch.object(vne, "async_session_factory", factory_mock):
        with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
            with patch.object(
                vne, "extract_voice_note", AsyncMock(side_effect=boom)
            ):
                with pytest.raises(RuntimeError, match="haiku exploded"):
                    await vne.process_voice_note(vn.id)

    # mark_processing_status was called with both 'processing' and 'failed'.
    statuses = [
        c.args[1] for c in mock_repo.mark_processing_status.await_args_list
    ]
    assert "processing" in statuses
    assert "failed" in statuses


async def test_process_voice_note_noise_marks_completed_without_storing():
    """Noise-triaged note: processing_status='completed', no entities stored."""
    vn = _make_voice_note(transcript="test test one two three")

    factory_mock = MagicMock()

    def factory_call(*_args, **_kwargs):
        cm = MagicMock()
        s = AsyncMock()
        s.commit = AsyncMock()
        s.rollback = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=s)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    factory_mock.side_effect = factory_call

    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=vn)
    mock_repo.mark_processing_status = AsyncMock()

    store_mock = AsyncMock()

    with patch.object(vne, "async_session_factory", factory_mock):
        with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
            with patch.object(
                vne, "extract_voice_note", AsyncMock(return_value=None)
            ):
                with patch.object(vne, "store_voice_note_extraction", store_mock):
                    result = await vne.process_voice_note(vn.id)

    assert result is True
    store_mock.assert_not_awaited()  # noise → no entity persistence
    statuses = [
        c.args[1] for c in mock_repo.mark_processing_status.await_args_list
    ]
    assert "completed" in statuses
    assert "failed" not in statuses


# ── process_pending_voice_notes scheduler hook ──────────────


async def test_process_pending_voice_notes_queries_pending_status():
    """The scheduler hook queries the repo with processing_status='pending'."""
    vn1 = _make_voice_note(voice_note_id=1)
    vn2 = _make_voice_note(voice_note_id=2)

    mock_repo = AsyncMock()
    mock_repo.list = AsyncMock(return_value=[vn1, vn2])

    factory_mock = MagicMock()

    def factory_call(*_args, **_kwargs):
        cm = MagicMock()
        s = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=s)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    factory_mock.side_effect = factory_call

    process_mock = AsyncMock(return_value=True)

    with patch.object(vne, "async_session_factory", factory_mock):
        with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
            with patch.object(vne, "process_voice_note", process_mock):
                count = await vne.process_pending_voice_notes()

    assert count == 2
    # Verify the filter passed to repo.list.
    list_kwargs = mock_repo.list.await_args.kwargs
    assert list_kwargs.get("processing_status") == "pending"
    # Each voice note went through process_voice_note.
    processed_ids = [c.args[0] for c in process_mock.await_args_list]
    assert processed_ids == [vn1.id, vn2.id]


async def test_process_pending_voice_notes_continues_on_error():
    """If one voice note's processing raises, the next one still runs."""
    vn1 = _make_voice_note(voice_note_id=1)
    vn2 = _make_voice_note(voice_note_id=2)

    mock_repo = AsyncMock()
    mock_repo.list = AsyncMock(return_value=[vn1, vn2])

    factory_mock = MagicMock()

    def factory_call(*_args, **_kwargs):
        cm = MagicMock()
        s = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=s)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    factory_mock.side_effect = factory_call

    side_effects = [RuntimeError("boom"), True]
    process_mock = AsyncMock(side_effect=side_effects)

    with patch.object(vne, "async_session_factory", factory_mock):
        with patch.object(vne, "VoiceNotesRepository", return_value=mock_repo):
            with patch.object(vne, "process_voice_note", process_mock):
                count = await vne.process_pending_voice_notes()

    # Second succeeded, first raised — loop kept going.
    assert count == 1
    assert process_mock.await_count == 2
