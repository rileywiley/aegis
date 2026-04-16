"""Tests for meeting extraction — mock Anthropic, verify parsing and storage."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.processing.meeting_extractor import (
    MeetingExtraction,
    extract_meeting,
    store_meeting_extraction,
)

# ── Fixtures ──────────────────────────────────────────────

CANNED_EXTRACTION = {
    "summary": "Team discussed Q3 budget and launch timeline.",
    "people": [
        {"name": "Alice Smith", "role": "PM", "email": "alice@example.com"},
        {"name": "Bob Jones", "role": "Engineer", "email": None},
    ],
    "action_items": [
        {
            "description": "Prepare budget proposal",
            "assignee": "Alice Smith",
            "deadline": "2026-04-20",
        },
        {
            "description": "Review technical feasibility",
            "assignee": "Bob Jones",
            "deadline": None,
        },
    ],
    "decisions": [
        {"description": "Cap budget at $280K", "decided_by": "Alice Smith"},
    ],
    "commitments": [
        {
            "description": "Deliver prototype by end of month",
            "committer": "Bob Jones",
            "recipient": "Alice Smith",
            "deadline": "2026-04-30",
        },
    ],
    "topics": ["Q3 budget", "launch timeline", "technical feasibility"],
    "sentiment": "positive",
}


def _make_mock_response(content_text: str, input_tokens: int = 500, output_tokens: int = 300):
    """Build a mock Anthropic response object."""
    content_block = MagicMock()
    content_block.text = content_text
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    response = MagicMock()
    response.content = [content_block]
    response.usage = usage
    return response


# ── Tests: extract_meeting ────────────────────────────────


@pytest.mark.asyncio
async def test_extract_meeting_parses_json():
    """extract_meeting should parse LLM JSON into a valid MeetingExtraction dict."""
    mock_response = _make_mock_response(json.dumps(CANNED_EXTRACTION))
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    mock_session = AsyncMock()

    with patch("aegis.processing.meeting_extractor.AsyncAnthropic", return_value=mock_client):
        with patch("aegis.processing.meeting_extractor.get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "test-key"
            result = await extract_meeting(
                session=mock_session,
                meeting_id=1,
                transcript_text="Alice: Let's discuss the budget...",
                attendee_names=["Alice Smith", "Bob Jones"],
            )

    # Validate it parses as MeetingExtraction
    extraction = MeetingExtraction(**result)
    assert extraction.summary == CANNED_EXTRACTION["summary"]
    assert len(extraction.action_items) == 2
    assert len(extraction.decisions) == 1
    assert len(extraction.commitments) == 1
    assert len(extraction.topics) == 3
    assert extraction.sentiment == "positive"


@pytest.mark.asyncio
async def test_extract_meeting_handles_markdown_wrapped_json():
    """extract_meeting should handle JSON wrapped in markdown code blocks."""
    wrapped = f"```json\n{json.dumps(CANNED_EXTRACTION)}\n```"
    mock_response = _make_mock_response(wrapped)
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    mock_session = AsyncMock()

    with patch("aegis.processing.meeting_extractor.AsyncAnthropic", return_value=mock_client):
        with patch("aegis.processing.meeting_extractor.get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "test-key"
            result = await extract_meeting(
                session=mock_session,
                meeting_id=1,
                transcript_text="Some transcript",
                attendee_names=[],
            )

    assert result["summary"] == CANNED_EXTRACTION["summary"]


@pytest.mark.asyncio
async def test_extract_meeting_tracks_usage():
    """extract_meeting should call _track_usage with token counts."""
    mock_response = _make_mock_response(json.dumps(CANNED_EXTRACTION), 100, 200)
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    mock_session = AsyncMock()

    with patch("aegis.processing.meeting_extractor.AsyncAnthropic", return_value=mock_client):
        with patch("aegis.processing.meeting_extractor.get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "test-key"
            with patch("aegis.processing.meeting_extractor._track_usage") as mock_track:
                mock_track.return_value = None
                await extract_meeting(
                    session=mock_session,
                    meeting_id=1,
                    transcript_text="Test",
                    attendee_names=[],
                )
                mock_track.assert_called_once_with(
                    mock_session, input_tokens=100, output_tokens=200
                )


# ── Tests: store_meeting_extraction ───────────────────────


@pytest.mark.asyncio
async def test_store_meeting_extraction_creates_entities():
    """store_meeting_extraction should call repo functions for each entity type."""
    extraction = {
        **CANNED_EXTRACTION,
        "_resolved_people": {
            "Alice Smith": 1,
            "Bob Jones": 2,
        },
    }

    mock_session = AsyncMock()

    mock_action_item = MagicMock()
    mock_action_item.id = 10
    mock_decision = MagicMock()
    mock_decision.id = 20
    mock_commitment = MagicMock()
    mock_commitment.id = 30
    mock_topic = MagicMock()
    mock_topic.id = 100

    with (
        patch("aegis.processing.meeting_extractor.embed_text", new_callable=AsyncMock) as mock_embed,
        patch("aegis.db.repositories.create_action_item", new_callable=AsyncMock) as mock_create_ai,
        patch("aegis.db.repositories.create_decision", new_callable=AsyncMock) as mock_create_dec,
        patch("aegis.db.repositories.create_commitment", new_callable=AsyncMock) as mock_create_com,
        patch("aegis.db.repositories.upsert_topic", new_callable=AsyncMock) as mock_upsert_topic,
        patch("aegis.db.repositories.link_meeting_topics", new_callable=AsyncMock) as mock_link,
        patch("aegis.db.repositories.update_meeting_extraction", new_callable=AsyncMock) as mock_update,
    ):
        mock_embed.return_value = [0.1] * 1536
        mock_create_ai.return_value = mock_action_item
        mock_create_dec.return_value = mock_decision
        mock_create_com.return_value = mock_commitment
        mock_upsert_topic.return_value = mock_topic

        await store_meeting_extraction(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )

        # 2 action items
        assert mock_create_ai.call_count == 2
        # Check first action item has correct assignee_id
        first_call_kwargs = mock_create_ai.call_args_list[0]
        assert first_call_kwargs.kwargs["assignee_id"] == 1  # Alice Smith
        assert first_call_kwargs.kwargs["source_meeting_id"] == 1

        # 1 decision
        assert mock_create_dec.call_count == 1

        # 1 commitment
        assert mock_create_com.call_count == 1
        com_kwargs = mock_create_com.call_args_list[0].kwargs
        assert com_kwargs["committer_id"] == 2  # Bob Jones
        assert com_kwargs["recipient_id"] == 1  # Alice Smith

        # 3 topics
        assert mock_upsert_topic.call_count == 3
        mock_link.assert_called_once()

        # Meeting updated
        mock_update.assert_called_once()


@pytest.mark.asyncio
async def test_store_extraction_without_resolved_people():
    """store_meeting_extraction should handle missing _resolved_people gracefully."""
    extraction = {**CANNED_EXTRACTION}  # no _resolved_people key

    mock_session = AsyncMock()
    mock_topic = MagicMock()
    mock_topic.id = 1

    with (
        patch("aegis.processing.meeting_extractor.embed_text", new_callable=AsyncMock) as mock_embed,
        patch("aegis.db.repositories.create_action_item", new_callable=AsyncMock),
        patch("aegis.db.repositories.create_decision", new_callable=AsyncMock),
        patch("aegis.db.repositories.create_commitment", new_callable=AsyncMock),
        patch("aegis.db.repositories.upsert_topic", new_callable=AsyncMock) as mock_topic_fn,
        patch("aegis.db.repositories.link_meeting_topics", new_callable=AsyncMock),
        patch("aegis.db.repositories.update_meeting_extraction", new_callable=AsyncMock),
    ):
        mock_embed.return_value = [0.1] * 1536
        mock_topic_fn.return_value = mock_topic

        # Should not raise
        await store_meeting_extraction(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )
