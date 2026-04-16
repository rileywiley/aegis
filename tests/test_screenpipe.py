"""Tests for Screenpipe client and meeting transcript building."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aegis.ingestion.meeting_detector import (
    MeetingDetector,
    _determine_status,
    _stitch_transcript,
)
from aegis.ingestion.screenpipe import ScreenpipeClient

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def audio_fixture():
    return _load_fixture("screenpipe_audio.json")


@pytest.fixture
def health_fixture():
    return _load_fixture("screenpipe_health.json")


@pytest.fixture
def screenpipe_client():
    return ScreenpipeClient(base_url="http://localhost:3030")


# ── ScreenpipeClient tests ──────────────────────────────────


class TestHealthCheck:
    async def test_health_check_up(self, screenpipe_client, health_fixture):
        """Health check returns True when Screenpipe responds with 200."""
        mock_response = httpx.Response(200, json=health_fixture)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await screenpipe_client.health_check()
        assert result is True

    async def test_health_check_down(self, screenpipe_client):
        """Health check returns False when Screenpipe is unreachable."""
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = await screenpipe_client.health_check()
        assert result is False

    async def test_health_check_timeout(self, screenpipe_client):
        """Health check returns False on timeout."""
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("Timed out"),
        ):
            result = await screenpipe_client.health_check()
        assert result is False

    async def test_health_check_500(self, screenpipe_client):
        """Health check returns False on server error."""
        mock_response = httpx.Response(500, json={"error": "internal"})

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await screenpipe_client.health_check()
        assert result is False


class TestAudioRetrieval:
    async def test_get_audio_success(self, screenpipe_client, audio_fixture):
        """Audio retrieval returns parsed chunks on success."""
        mock_response = httpx.Response(200, json=audio_fixture, request=httpx.Request("GET", "http://localhost:3030/search"))
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            chunks = await screenpipe_client.get_audio(start, end)

        assert len(chunks) == 8
        assert chunks[0]["content"]["text"].startswith("Good morning")
        assert chunks[0]["content"]["speaker"]["name"] == "Alice"

    async def test_get_audio_empty(self, screenpipe_client):
        """Audio retrieval returns empty list when no results."""
        mock_response = httpx.Response(200, json={"data": []}, request=httpx.Request("GET", "http://localhost:3030/search"))
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            chunks = await screenpipe_client.get_audio(start, end)

        assert chunks == []

    async def test_get_audio_connection_error(self, screenpipe_client):
        """Audio retrieval returns empty list on connection error."""
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc)

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            chunks = await screenpipe_client.get_audio(start, end)

        assert chunks == []

    async def test_get_audio_timeout(self, screenpipe_client):
        """Audio retrieval returns empty list on timeout."""
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc)

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("Timed out"),
        ):
            chunks = await screenpipe_client.get_audio(start, end)

        assert chunks == []


class TestScreenOCR:
    async def test_get_screen_ocr_success(self, screenpipe_client):
        """OCR retrieval returns results on success."""
        ocr_data = {
            "data": [
                {
                    "type": "OCR",
                    "content": {
                        "text": "Q2 Roadmap Presentation - Slide 3",
                        "app_name": "Microsoft PowerPoint",
                        "timestamp": "2026-04-15T10:05:00Z",
                    },
                }
            ]
        }
        mock_response = httpx.Response(200, json=ocr_data, request=httpx.Request("GET", "http://localhost:3030/search"))
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            frames = await screenpipe_client.get_screen_ocr(start, end)

        assert len(frames) == 1
        assert frames[0]["content"]["app_name"] == "Microsoft PowerPoint"


# ── Transcript stitching tests ──────────────────────────────


class TestStitchTranscript:
    def test_stitch_with_speaker_names(self, audio_fixture):
        """Transcript stitching produces speaker-labeled lines."""
        chunks = audio_fixture["data"]
        transcript = _stitch_transcript(chunks)

        assert "[Alice]:" in transcript
        assert "[Bob]:" in transcript
        assert "[Carol]:" in transcript
        assert "Q2 roadmap" in transcript

    def test_stitch_empty(self):
        """Empty chunks produce empty transcript."""
        assert _stitch_transcript([]) == ""

    def test_stitch_with_string_speaker(self):
        """Handles speaker as plain string."""
        chunks = [
            {
                "content": {
                    "text": "Hello there",
                    "timestamp": "2026-04-15T10:00:00Z",
                    "speaker": "Jane",
                }
            }
        ]
        transcript = _stitch_transcript(chunks)
        assert "[Jane]: Hello there" in transcript

    def test_stitch_with_missing_speaker(self):
        """Falls back to 'Speaker' label when speaker info is missing."""
        chunks = [
            {
                "content": {
                    "text": "Testing without speaker",
                    "timestamp": "2026-04-15T10:00:00Z",
                }
            }
        ]
        transcript = _stitch_transcript(chunks)
        assert "[Unknown]: Testing without speaker" in transcript


# ── Transcript status tests ─────────────────────────────────


class TestDetermineStatus:
    def test_captured_good_coverage(self, audio_fixture):
        """Status is 'captured' when audio covers most of the meeting."""
        chunks = audio_fixture["data"]
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 10, 10, tzinfo=timezone.utc)

        status = _determine_status(chunks, start, end)
        assert status == "captured"

    def test_no_audio(self):
        """Status is 'no_audio' when there are no chunks."""
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc)

        status = _determine_status([], start, end)
        assert status == "no_audio"

    def test_partial_sparse_coverage(self):
        """Status is 'partial' when audio coverage is sparse."""
        chunks = [
            {
                "content": {
                    "text": "Just one chunk",
                    "timestamp": "2026-04-15T10:00:00Z",
                    "speaker": "Alice",
                }
            }
        ]
        start = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc)

        status = _determine_status(chunks, start, end)
        assert status == "partial"


# ── MeetingDetector tests ───────────────────────────────────


class _FakeMeeting:
    """Lightweight stand-in for Meeting ORM model in unit tests."""

    def __init__(self, id, title, start_time, end_time, is_excluded=False):
        self.id = id
        self.title = title
        self.start_time = start_time
        self.end_time = end_time
        self.transcript_status = "pending"
        self.is_excluded = is_excluded


class TestBuildTranscript:
    async def test_build_transcript_captured(self, audio_fixture):
        """build_transcript sets transcript and status when audio exists."""
        meeting = _FakeMeeting(
            id=1,
            title="Q2 Planning",
            start_time=datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 4, 15, 10, 10, tzinfo=timezone.utc),
        )

        mock_screenpipe = AsyncMock(spec=ScreenpipeClient)
        mock_screenpipe.get_audio = AsyncMock(return_value=audio_fixture["data"])

        detector = MeetingDetector(screenpipe=mock_screenpipe)

        mock_session = AsyncMock()
        # Mock the adjacent meetings query to return empty
        mock_result = AsyncMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("aegis.ingestion.meeting_detector.repositories") as mock_repos:
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(mock_session, meeting)

            mock_repos.update_meeting_transcript.assert_called_once()
            call_args = mock_repos.update_meeting_transcript.call_args
            # Called as positional: (session, meeting_id, transcript_text=..., transcript_status=...)
            assert call_args.kwargs["transcript_status"] == "captured"
            assert "[Alice]:" in call_args.kwargs["transcript_text"]

    async def test_build_transcript_no_audio(self):
        """build_transcript sets no_audio when Screenpipe returns nothing."""
        meeting = _FakeMeeting(
            id=2,
            title="Silent Meeting",
            start_time=datetime(2026, 4, 15, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 4, 15, 15, 0, tzinfo=timezone.utc),
        )

        mock_screenpipe = AsyncMock(spec=ScreenpipeClient)
        mock_screenpipe.get_audio = AsyncMock(return_value=[])

        detector = MeetingDetector(screenpipe=mock_screenpipe)
        mock_session = AsyncMock()

        with patch("aegis.ingestion.meeting_detector.repositories") as mock_repos:
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(mock_session, meeting)

            mock_repos.update_meeting_transcript.assert_called_once_with(
                mock_session, 2, transcript_text="", transcript_status="no_audio"
            )


class TestBackToBackPadding:
    async def test_adjacent_meeting_truncates_padding(self, audio_fixture):
        """When meetings are back-to-back, padding is truncated to midpoint."""
        # Meeting A: 10:00-10:30, Meeting B (ours): 10:32-11:00
        # Gap is 2 min < 5 min threshold, so padding should be truncated
        meeting_a = _FakeMeeting(
            id=10,
            title="Meeting A",
            start_time=datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc),
        )
        meeting_b = _FakeMeeting(
            id=11,
            title="Meeting B",
            start_time=datetime(2026, 4, 15, 10, 32, tzinfo=timezone.utc),
            end_time=datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc),
        )

        mock_screenpipe = AsyncMock(spec=ScreenpipeClient)
        mock_screenpipe.get_audio = AsyncMock(return_value=audio_fixture["data"])

        detector = MeetingDetector(screenpipe=mock_screenpipe)
        mock_session = AsyncMock()

        with patch("aegis.ingestion.meeting_detector.repositories") as mock_repos:
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[meeting_a, meeting_b])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(mock_session, meeting_b)

            # Verify get_audio was called — the start window should have been
            # truncated (midpoint of 10:30 and 10:32 = 10:31)
            call_args = mock_screenpipe.get_audio.call_args_list[0]
            actual_start = call_args[0][0]  # first positional arg
            midpoint = datetime(2026, 4, 15, 10, 31, tzinfo=timezone.utc)
            assert actual_start >= midpoint

    async def test_no_adjacent_full_padding(self, audio_fixture):
        """When no adjacent meetings, full 5-min padding is used."""
        meeting = _FakeMeeting(
            id=20,
            title="Solo Meeting",
            start_time=datetime(2026, 4, 15, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 4, 15, 15, 0, tzinfo=timezone.utc),
        )

        mock_screenpipe = AsyncMock(spec=ScreenpipeClient)
        mock_screenpipe.get_audio = AsyncMock(return_value=audio_fixture["data"])

        detector = MeetingDetector(screenpipe=mock_screenpipe)
        mock_session = AsyncMock()

        with patch("aegis.ingestion.meeting_detector.repositories") as mock_repos:
            mock_repos.get_meetings_for_range = AsyncMock(return_value=[meeting])
            mock_repos.update_meeting_transcript = AsyncMock()

            await detector.build_transcript(mock_session, meeting)

            call_args = mock_screenpipe.get_audio.call_args_list[0]
            actual_start = call_args[0][0]
            expected_start = meeting.start_time - timedelta(minutes=5)
            assert actual_start == expected_start
