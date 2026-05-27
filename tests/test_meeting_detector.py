"""Tests for the rewritten ``aegis/ingestion/meeting_detector.py``.

The legacy chunk-stitching path is gone (Helios returns pre-stitched
segments), so tests now exercise:

  * status mapping from coverage_pct → 'captured' / 'partial' / 'no_audio'
  * back-to-back meeting buffer truncation (still calendar-domain logic)
  * the "Helios unreachable" fallback branch
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from aegis.ingestion.helios import HeliosTranscript, HeliosTranscriptSegment
from aegis.ingestion.meeting_detector import (
    BUFFER_MINUTES,
    MeetingDetector,
    _status_from_coverage,
)


class _FakeMeeting:
    """Lightweight stand-in for the ``Meeting`` ORM model."""

    def __init__(self, id, title, start_time, end_time, is_excluded=False):
        self.id = id
        self.title = title
        self.start_time = start_time
        self.end_time = end_time
        self.transcript_status = "pending"
        self.is_excluded = is_excluded


def _segment(start_dt: datetime, dur_seconds: float, *, text="hi", speaker="user"):
    return HeliosTranscriptSegment(
        start_ts=start_dt.timestamp(),
        end_ts=start_dt.timestamp() + dur_seconds,
        text=text,
        speaker=speaker,
    )


# ── _status_from_coverage ────────────────────────────────────


class TestStatusFromCoverage:
    def test_captured_at_threshold(self):
        seg = _segment(datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc), 1.0)
        assert _status_from_coverage(0.8, [seg]) == "captured"

    def test_captured_above_threshold(self):
        seg = _segment(datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc), 1.0)
        assert _status_from_coverage(1.0, [seg]) == "captured"

    def test_partial_below_threshold(self):
        seg = _segment(datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc), 1.0)
        assert _status_from_coverage(0.5, [seg]) == "partial"

    def test_partial_just_above_zero(self):
        seg = _segment(datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc), 1.0)
        assert _status_from_coverage(0.05, [seg]) == "partial"

    def test_no_audio_zero_coverage(self):
        assert _status_from_coverage(0.0, []) == "no_audio"

    def test_no_audio_when_segments_empty(self):
        assert _status_from_coverage(0.99, []) == "no_audio"


# ── build_transcript ─────────────────────────────────────────


class TestBuildTranscriptCaptured:
    async def test_full_coverage_marks_captured(self):
        meeting = _FakeMeeting(
            id=1,
            title="Q2 Planning",
            start_time=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc),
        )
        # A single segment that spans the whole meeting → coverage_pct = 1.0
        transcript = HeliosTranscript(
            segments=[
                _segment(meeting.start_time, 1800.0, text="hello", speaker="user")
            ],
            coverage_pct=1.0,
        )

        helios_client = AsyncMock()
        detector = MeetingDetector(helios=helios_client)
        session = AsyncMock()

        with (
            patch(
                "aegis.ingestion.meeting_detector.get_transcript_for_meeting",
                new_callable=AsyncMock,
                return_value=transcript,
            ),
            patch(
                "aegis.ingestion.meeting_detector.repositories"
            ) as mock_repos,
        ):
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(session, meeting)

            mock_repos.update_meeting_transcript.assert_awaited_once()
            kwargs = mock_repos.update_meeting_transcript.call_args.kwargs
            assert kwargs["transcript_status"] == "captured"
            assert "user: hello" in kwargs["transcript_text"]


class TestBuildTranscriptPartial:
    async def test_partial_coverage_marks_partial(self):
        meeting = _FakeMeeting(
            id=2,
            title="Sparse Meeting",
            start_time=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc),
        )
        transcript = HeliosTranscript(
            segments=[
                _segment(meeting.start_time, 60.0, text="brief", speaker="user")
            ],
            coverage_pct=0.3,
        )

        helios_client = AsyncMock()
        detector = MeetingDetector(helios=helios_client)
        session = AsyncMock()

        with (
            patch(
                "aegis.ingestion.meeting_detector.get_transcript_for_meeting",
                new_callable=AsyncMock,
                return_value=transcript,
            ),
            patch(
                "aegis.ingestion.meeting_detector.repositories"
            ) as mock_repos,
        ):
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(session, meeting)

            kwargs = mock_repos.update_meeting_transcript.call_args.kwargs
            assert kwargs["transcript_status"] == "partial"


class TestBuildTranscriptNoAudio:
    async def test_zero_coverage_no_segments(self):
        meeting = _FakeMeeting(
            id=3,
            title="Silent Meeting",
            start_time=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc),
        )
        transcript = HeliosTranscript(segments=[], coverage_pct=0.0)

        helios_client = AsyncMock()
        detector = MeetingDetector(helios=helios_client)
        session = AsyncMock()

        with (
            patch(
                "aegis.ingestion.meeting_detector.get_transcript_for_meeting",
                new_callable=AsyncMock,
                return_value=transcript,
            ),
            patch(
                "aegis.ingestion.meeting_detector.repositories"
            ) as mock_repos,
        ):
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(session, meeting)

            kwargs = mock_repos.update_meeting_transcript.call_args.kwargs
            assert kwargs["transcript_status"] == "no_audio"
            assert kwargs["transcript_text"] == ""

    async def test_helios_unreachable_marks_no_audio(self):
        """When Helios returns None (daemon down), persist no_audio."""
        meeting = _FakeMeeting(
            id=4,
            title="During Outage",
            start_time=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc),
        )

        helios_client = AsyncMock()
        detector = MeetingDetector(helios=helios_client)
        session = AsyncMock()

        with (
            patch(
                "aegis.ingestion.meeting_detector.get_transcript_for_meeting",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "aegis.ingestion.meeting_detector.repositories"
            ) as mock_repos,
        ):
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(session, meeting)

            kwargs = mock_repos.update_meeting_transcript.call_args.kwargs
            assert kwargs["transcript_status"] == "no_audio"
            assert kwargs["transcript_text"] == ""


# ── back-to-back buffer truncation ───────────────────────────


class TestBackToBackPadding:
    async def test_adjacent_meeting_truncates_padding(self):
        meeting_a = _FakeMeeting(
            id=10,
            title="Meeting A",
            start_time=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc),
        )
        meeting_b = _FakeMeeting(
            id=11,
            title="Meeting B",
            start_time=datetime(2026, 5, 2, 14, 32, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc),
        )

        helios_client = AsyncMock()
        detector = MeetingDetector(helios=helios_client)
        session = AsyncMock()

        captured_args: dict = {}

        async def fake_get(client, start, end, *, include_words=False):
            # Capture the first call's window to verify truncation.
            captured_args.setdefault("start", start)
            captured_args.setdefault("end", end)
            return HeliosTranscript(segments=[], coverage_pct=0.0)

        with (
            patch(
                "aegis.ingestion.meeting_detector.get_transcript_for_meeting",
                side_effect=fake_get,
            ),
            patch(
                "aegis.ingestion.meeting_detector.repositories"
            ) as mock_repos,
        ):
            mock_repos.get_meetings_for_range = AsyncMock(
                return_value=[meeting_a, meeting_b]
            )
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(session, meeting_b)

            # Midpoint of 14:30 and 14:32 = 14:31
            midpoint = datetime(2026, 5, 2, 14, 31, tzinfo=timezone.utc)
            assert captured_args["start"] >= midpoint

    async def test_no_adjacent_full_padding(self):
        meeting = _FakeMeeting(
            id=20,
            title="Solo Meeting",
            start_time=datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 2, 17, 0, tzinfo=timezone.utc),
        )

        helios_client = AsyncMock()
        detector = MeetingDetector(helios=helios_client)
        session = AsyncMock()

        captured_args: dict = {}

        async def fake_get(client, start, end, *, include_words=False):
            captured_args.setdefault("start", start)
            captured_args.setdefault("end", end)
            return HeliosTranscript(segments=[], coverage_pct=0.0)

        with (
            patch(
                "aegis.ingestion.meeting_detector.get_transcript_for_meeting",
                side_effect=fake_get,
            ),
            patch(
                "aegis.ingestion.meeting_detector.repositories"
            ) as mock_repos,
        ):
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[meeting])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(session, meeting)

            expected_start = meeting.start_time - timedelta(minutes=BUFFER_MINUTES)
            assert captured_args["start"] == expected_start
