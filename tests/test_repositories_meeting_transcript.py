"""Tests for ``update_meeting_transcript`` — Phase 3 §12.4 follow-up.

Specifically guards against a regression where a transcript would land
on a meeting row but ``processing_status`` would stay NULL, causing the
extraction filter (``processing_status IN ('pending','failed')``) to
silently skip Helios-fed meetings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from aegis.db.repositories import update_meeting_transcript


def _captured_values(session_mock: AsyncMock) -> dict:
    """Pull the ``.values`` dict off the Update statement passed to execute().

    Maps Column → bound value. SQLAlchemy stores this on Update._values
    as an immutabledict of Column → BindParameter.
    """
    args, _ = session_mock.execute.call_args
    stmt = args[0]
    return {col.name: bind.value for col, bind in stmt._values.items()}


class TestUpdateMeetingTranscript:
    async def test_captured_seeds_processing_status_pending(self):
        session = AsyncMock()
        await update_meeting_transcript(
            session,
            meeting_id=1,
            transcript_text="hello world",
            transcript_status="captured",
        )
        values = _captured_values(session)
        assert values["transcript_text"] == "hello world"
        assert values["transcript_status"] == "captured"
        assert values["processing_status"] == "pending"

    async def test_partial_seeds_processing_status_pending(self):
        session = AsyncMock()
        await update_meeting_transcript(
            session,
            meeting_id=2,
            transcript_text="brief",
            transcript_status="partial",
        )
        values = _captured_values(session)
        assert values["processing_status"] == "pending"

    async def test_no_audio_does_not_seed_processing_status(self):
        session = AsyncMock()
        await update_meeting_transcript(
            session,
            meeting_id=3,
            transcript_text="",
            transcript_status="no_audio",
        )
        values = _captured_values(session)
        assert "processing_status" not in values

    async def test_empty_transcript_with_captured_skips_seed(self):
        session = AsyncMock()
        await update_meeting_transcript(
            session,
            meeting_id=4,
            transcript_text="",
            transcript_status="captured",
        )
        values = _captured_values(session)
        # No real transcript → no extraction work to queue.
        assert "processing_status" not in values
