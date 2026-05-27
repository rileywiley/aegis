"""Tests for the processing pipeline."""

from unittest.mock import AsyncMock, patch

import pytest

from aegis.processing.pipeline import PipelineState, build_pipeline, route_by_type


def test_route_by_type_meeting():
    state = PipelineState(item_id=1, item_type="meeting", transcript_text="test")
    assert route_by_type(state) == "extract_meeting"


def test_route_by_type_voice_note():
    state = PipelineState(item_id=1, item_type="voice_note")
    assert route_by_type(state) == "extract_voice_note"


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


# ── Voice note pipeline integration (Wave 4L) ──────────────


def test_pipeline_includes_voice_note_node():
    """The compiled graph should include the extract_voice_note node."""
    graph = build_pipeline()
    # LangGraph stores nodes on .nodes (a dict-like).
    node_keys = set(graph.nodes.keys())
    assert "extract_voice_note" in node_keys
    assert "extract_meeting" in node_keys


async def test_extract_voice_note_node_calls_processor():
    """extract_voice_note_node delegates to voice_note_extractor.process_voice_note."""
    from aegis.processing.pipeline import extract_voice_note_node

    state = PipelineState(item_id=42, item_type="voice_note")
    process_mock = AsyncMock(return_value=True)

    with patch(
        "aegis.processing.voice_note_extractor.process_voice_note",
        process_mock,
    ):
        result = await extract_voice_note_node(state)

    process_mock.assert_awaited_once_with(42)
    assert result.get("error") is None
    assert result.get("extraction_result") == {"_voice_note_done": True}


async def test_extract_voice_note_node_records_error_on_failure():
    """If process_voice_note raises, the node returns {"error": ...}."""
    from aegis.processing.pipeline import extract_voice_note_node

    state = PipelineState(item_id=7, item_type="voice_note")
    process_mock = AsyncMock(side_effect=RuntimeError("kaboom"))

    with patch(
        "aegis.processing.voice_note_extractor.process_voice_note",
        process_mock,
    ):
        result = await extract_voice_note_node(state)

    assert "kaboom" in (result.get("error") or "")


async def test_process_pending_voice_notes_delegates_to_extractor():
    """The pipeline-level scheduler hook delegates to the extractor's runner."""
    from aegis.processing.pipeline import process_pending_voice_notes

    runner_mock = AsyncMock(return_value=3)
    with patch(
        "aegis.processing.voice_note_extractor.process_pending_voice_notes",
        runner_mock,
    ):
        count = await process_pending_voice_notes()

    runner_mock.assert_awaited_once()
    # The wrapper passes through the limit kwarg.
    assert runner_mock.await_args.kwargs.get("limit") == 50
    assert count == 3


async def test_process_pending_items_runs_both_runners():
    """process_pending_items runs meetings + voice notes and returns counts."""
    from aegis.processing.pipeline import process_pending_items

    with patch(
        "aegis.processing.pipeline.process_pending_meetings",
        AsyncMock(return_value=4),
    ):
        with patch(
            "aegis.processing.pipeline.process_pending_voice_notes",
            AsyncMock(return_value=2),
        ):
            counts = await process_pending_items()

    assert counts == {"meetings": 4, "voice_notes": 2}
