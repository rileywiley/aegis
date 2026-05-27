"""Tests for ``aegis/ingestion/helios.py`` — the high-level Helios wrapper.

The underlying ``HeliosClient`` is exercised directly in
``test_helios_client.py``; here we cover only the dataclass parsing,
``coverage_pct`` math, and ``joined_text`` formatting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from aegis.ingestion.helios import (
    HeliosTranscript,
    HeliosTranscriptSegment,
    get_ocr_for_meeting,
    get_transcript_for_meeting,
    health_check,
)


def _make_client(payload):
    """Return an AsyncMock HeliosClient whose audio call yields ``payload``."""
    client = AsyncMock()
    client.get_transcript_for_meeting = AsyncMock(return_value=payload)
    client.get_ocr = AsyncMock(return_value=payload)
    client.health_check = AsyncMock(return_value=True)
    return client


# ── Dataclass / formatting ───────────────────────────────────


class TestJoinedText:
    def test_speaker_labels_and_newlines(self):
        t = HeliosTranscript(
            segments=[
                HeliosTranscriptSegment(
                    start_ts=0.0, end_ts=3.0, text="hello", speaker="user"
                ),
                HeliosTranscriptSegment(
                    start_ts=3.0,
                    end_ts=6.0,
                    text="hi there",
                    speaker="SPEAKER_00",
                ),
            ]
        )
        assert t.joined_text == "user: hello\nSPEAKER_00: hi there"

    def test_missing_speaker_falls_back_to_question_mark(self):
        t = HeliosTranscript(
            segments=[
                HeliosTranscriptSegment(
                    start_ts=0.0, end_ts=1.0, text="anonymous", speaker=None
                ),
            ]
        )
        assert t.joined_text == "?: anonymous"

    def test_empty_text_segments_skipped(self):
        t = HeliosTranscript(
            segments=[
                HeliosTranscriptSegment(
                    start_ts=0.0, end_ts=1.0, text="", speaker="user"
                ),
                HeliosTranscriptSegment(
                    start_ts=1.0, end_ts=2.0, text="real", speaker="user"
                ),
            ]
        )
        assert t.joined_text == "user: real"

    def test_empty_segments_yields_empty_string(self):
        assert HeliosTranscript(segments=[]).joined_text == ""


# ── get_transcript_for_meeting ───────────────────────────────


class TestGetTranscriptForMeeting:
    async def test_unreachable_returns_none(self):
        client = AsyncMock()
        client.get_transcript_for_meeting = AsyncMock(return_value=None)

        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        result = await get_transcript_for_meeting(client, start, end)
        assert result is None

    async def test_full_coverage_clamped_to_one(self):
        # Meeting is 60s long but Helios returned 120s of segments —
        # coverage must clamp to 1.0.
        start = datetime(2026, 5, 2, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 14, 1, 0, tzinfo=timezone.utc)
        payload = {
            "segments": [
                {
                    "start": start.timestamp(),
                    "end": start.timestamp() + 60.0,
                    "text": "first half",
                    "speaker": "user",
                },
                {
                    "start": start.timestamp() + 60.0,
                    "end": start.timestamp() + 120.0,
                    "text": "second",
                    "speaker": "SPEAKER_00",
                },
            ]
        }
        client = _make_client(payload)
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert len(result.segments) == 2
        assert result.coverage_pct == 1.0

    async def test_partial_coverage_proportional(self):
        # 60s meeting, Helios returned 30s of segments → coverage = 0.5
        start = datetime(2026, 5, 2, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 14, 1, 0, tzinfo=timezone.utc)
        payload = {
            "segments": [
                {
                    "start": start.timestamp(),
                    "end": start.timestamp() + 30.0,
                    "text": "half",
                    "speaker": "user",
                }
            ]
        }
        client = _make_client(payload)
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert pytest.approx(result.coverage_pct, abs=1e-6) == 0.5

    async def test_zero_coverage_when_no_segments(self):
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        client = _make_client({"segments": []})
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert result.coverage_pct == 0.0
        assert result.segments == []

    async def test_zero_coverage_when_payload_missing_segments_key(self):
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        client = _make_client({})  # no "segments" key
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert result.segments == []
        assert result.coverage_pct == 0.0

    async def test_include_words_propagated(self):
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 14, 0, 30, tzinfo=timezone.utc)
        payload = {
            "segments": [
                {
                    "start": start.timestamp(),
                    "end": start.timestamp() + 5.0,
                    "text": "hello",
                    "speaker": "user",
                    "words": [{"w": "hello", "ts": start.timestamp()}],
                }
            ]
        }
        client = _make_client(payload)

        result = await get_transcript_for_meeting(
            client, start, end, include_words=True
        )
        assert result is not None
        assert result.segments[0].words == [
            {"w": "hello", "ts": start.timestamp()}
        ]
        client.get_transcript_for_meeting.assert_awaited_once_with(
            start, end, include_words=True
        )

    async def test_include_words_false_drops_words(self):
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 14, 0, 30, tzinfo=timezone.utc)
        payload = {
            "segments": [
                {
                    "start": start.timestamp(),
                    "end": start.timestamp() + 5.0,
                    "text": "hi",
                    "speaker": "user",
                    "words": [{"w": "hi", "ts": start.timestamp()}],
                }
            ]
        }
        client = _make_client(payload)
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert result.segments[0].words is None

    async def test_malformed_segments_dropped_silently(self):
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 14, 1, 0, tzinfo=timezone.utc)
        payload = {
            "segments": [
                {"text": "no timestamps"},  # missing start/end
                {
                    "start": start.timestamp(),
                    "end": start.timestamp() + 10.0,
                    "text": "ok",
                    "speaker": "user",
                },
            ]
        }
        client = _make_client(payload)
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert len(result.segments) == 1
        assert result.segments[0].text == "ok"


# ── Contract: helios serializes segments with start/end keys ─


class TestSegmentContract:
    """Lock the field-name contract: helios serializes transcript segments
    with ``start`` / ``end`` keys (per HELIOS.md §7.4 — see
    ``helios.api.schemas.TranscriptSegmentResponse``). Aegis ingestion
    must read those exact keys, not ``start_ts`` / ``end_ts``. If the
    helios schema ever changes, this test fails loudly instead of
    silently dropping every segment.
    """

    async def test_parser_accepts_helios_dict_shape(self):
        # Hand-built dict matching ``TranscriptSegmentResponse.model_dump()``
        # output. The actual model lives in helios/ which is not installed
        # in the aegis venv; the parallel helios-side test
        # ``test_segment_response_keys_locked`` asserts the model
        # serializes to exactly these keys.
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 14, 0, 30, tzinfo=timezone.utc)
        helios_serialized = {
            "segments": [
                {
                    "start": start.timestamp(),
                    "end": start.timestamp() + 5.0,
                    "speaker": "user",
                    "text": "contract-locked",
                    "words": None,
                }
            ]
        }
        client = _make_client(helios_serialized)
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert len(result.segments) == 1
        assert result.segments[0].start_ts == start.timestamp()
        assert result.segments[0].end_ts == start.timestamp() + 5.0
        assert result.segments[0].text == "contract-locked"

    async def test_old_field_names_rejected(self):
        # Defensive — if a caller accidentally hands us the legacy field
        # names, the segment is dropped (we don't silently coerce).
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 14, 0, 30, tzinfo=timezone.utc)
        wrong = {
            "segments": [
                {
                    "start_ts": start.timestamp(),
                    "end_ts": start.timestamp() + 5.0,
                    "text": "wrong shape",
                    "speaker": "user",
                }
            ]
        }
        client = _make_client(wrong)
        result = await get_transcript_for_meeting(client, start, end)
        assert result is not None
        assert result.segments == []  # malformed → dropped


# ── get_ocr_for_meeting + health_check passthroughs ──────────


class TestPassThroughs:
    async def test_get_ocr_for_meeting_passthrough(self):
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        payload = {"frames": []}
        client = _make_client(payload)
        result = await get_ocr_for_meeting(client, start, end)
        assert result == payload
        client.get_ocr.assert_awaited_once_with(start, end)

    async def test_health_check_passthrough_true(self):
        client = _make_client({"segments": []})
        client.health_check = AsyncMock(return_value=True)
        assert await health_check(client) is True

    async def test_health_check_passthrough_false(self):
        client = _make_client({"segments": []})
        client.health_check = AsyncMock(return_value=False)
        assert await health_check(client) is False
