"""Contract test: ``TranscriptSegmentResponse`` field-name lock.

Aegis ingestion (``aegis/ingestion/helios.py``) reads ``s["start"]`` and
``s["end"]`` from the dict that helios serializes for ``/v1/audio`` and
``/v1/sessions/{id}/transcript``. This test pins the helios-side
serialization so any rename ("start_ts"/"end_ts") fails here loudly
instead of silently dropping every Aegis-side segment via the
KeyError-swallowing branch.

Companion: ``aegis/tests/test_helios_ingestion.py::TestSegmentContract``.
"""

from __future__ import annotations

from helios.api.schemas import TranscriptResponse, TranscriptSegmentResponse


def test_segment_response_keys_locked():
    seg = TranscriptSegmentResponse(
        start=1.5, end=2.5, text="hi", speaker="user", words=None,
    )
    payload = seg.model_dump()
    # Required keys for Aegis ingestion.
    assert "start" in payload
    assert "end" in payload
    assert payload["start"] == 1.5
    assert payload["end"] == 2.5
    # Wrong field names MUST NOT appear — Aegis would silently drop them.
    assert "start_ts" not in payload
    assert "end_ts" not in payload


def test_transcript_response_carries_segments_through():
    """Ensure the wrapping ``TranscriptResponse`` doesn't strip keys."""
    resp = TranscriptResponse(
        session_id=1,
        started_at=0.0,
        ended_at=10.0,
        segments=[
            TranscriptSegmentResponse(
                start=1.0, end=2.0, text="a", speaker="user", words=None,
            ),
        ],
    )
    payload = resp.model_dump()
    assert payload["segments"][0]["start"] == 1.0
    assert payload["segments"][0]["end"] == 2.0
    assert "start_ts" not in payload["segments"][0]
    assert "end_ts" not in payload["segments"][0]
