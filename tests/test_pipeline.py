"""Tests for the processing pipeline."""

from unittest.mock import AsyncMock, patch

import pytest

from aegis.processing.pipeline import PipelineState, build_pipeline, route_by_type


def test_route_by_type_meeting():
    state = PipelineState(item_id=1, item_type="meeting", transcript_text="test")
    assert route_by_type(state) == "extract_meeting"


def test_route_by_type_unknown():
    state = PipelineState(item_id=1, item_type="email")
    assert route_by_type(state) == "end"


def test_pipeline_builds():
    """Pipeline graph compiles without errors."""
    graph = build_pipeline()
    compiled = graph.compile()
    assert compiled is not None


def test_pipeline_state_model():
    """PipelineState validates correctly."""
    state = PipelineState(
        item_id=42,
        item_type="meeting",
        transcript_text="Hello world",
        attendee_names=["Alice", "Bob"],
    )
    assert state.item_id == 42
    assert state.item_type == "meeting"
    assert len(state.attendee_names) == 2
    assert state.extraction_result is None
    assert state.error is None


async def test_process_meeting_skips_no_transcript():
    """process_meeting returns False for meetings without transcripts."""
    from aegis.processing.pipeline import process_meeting

    mock_meeting = AsyncMock()
    mock_meeting.transcript_text = None
    mock_meeting.processing_status = "pending"
    mock_meeting.last_extracted_at = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_meeting)

    with patch("aegis.processing.pipeline.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await process_meeting(1)

    assert result is False


async def test_process_meeting_skips_already_extracted():
    """process_meeting returns True for already-extracted meetings."""
    from datetime import datetime, timezone
    from aegis.processing.pipeline import process_meeting

    mock_meeting = AsyncMock()
    mock_meeting.transcript_text = "Some transcript"
    mock_meeting.processing_status = "completed"
    mock_meeting.last_extracted_at = datetime.now(timezone.utc)

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_meeting)

    with patch("aegis.processing.pipeline.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await process_meeting(1)

    assert result is True
