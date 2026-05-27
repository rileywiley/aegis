"""``/v1/audio`` — cross-session transcript-segment time-window query.

Per HELIOS.md §9.6: returns ``transcript_segments`` whose
``[start_ts, end_ts]`` intersects the requested ``?start=&end=``
window, across all sessions in that range. Wave 3I made this real
(was a Phase-2 placeholder returning ``{chunks: []}``). The Aegis
``HeliosClient.get_transcript_for_meeting`` calls this endpoint when
a meeting crosses a session boundary — typically two back-to-back
captures sandwiching the scheduled meeting end.

``/v1/sessions/{id}/transcript`` (sessions.py) is the same data
filtered by session id; this endpoint is the time-window dimension.

Wave 6 switched the response shape from ``AudioListResponse`` to
``TranscriptResponse`` to match HELIOS.md §9.6 spec — the ``coverage``
object is now populated with the sum of merged segment-interval lengths
clamped to the requested window. The legacy ``chunks`` field is gone.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request, status

from helios.api.schemas import (
    TranscriptCoverage,
    TranscriptResponse,
    TranscriptSegmentResponse,
)
from helios.db import queries
from helios.log import get_logger

router = APIRouter()
log = get_logger("api.audio")


def _err(code: int, error: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"error": error, "detail": detail, "code": code},
    )


def _decode_words(words: str | None) -> list | None:
    """Parse the JSON ``words`` column to a list, swallowing bad JSON."""
    if not words:
        return None
    try:
        decoded = json.loads(words)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, list):
        return None
    return decoded


def _captured_seconds(
    segments,
    window_start: float,
    window_end: float,
) -> float:
    """Return the union (no double-counting) of segment intervals
    clamped to ``[window_start, window_end]``.

    Cross-session ``/v1/audio`` queries don't have a single session
    boundary, so per-chunk coverage (which sessions.py uses) is not
    available here. We approximate by merging overlapping segments —
    this matches what an Aegis caller would compute itself from
    segment durations, but lets us return it inline per spec §9.6.
    """
    intervals: list[tuple[float, float]] = []
    for s in segments:
        a = max(window_start, float(s.start_ts))
        b = min(window_end, float(s.end_ts))
        if b > a:
            intervals.append((a, b))
    if not intervals:
        return 0.0
    intervals.sort()
    merged_start, merged_end = intervals[0]
    total = 0.0
    for a, b in intervals[1:]:
        if a <= merged_end:
            if b > merged_end:
                merged_end = b
        else:
            total += merged_end - merged_start
            merged_start, merged_end = a, b
    total += merged_end - merged_start
    return total


@router.get("/audio", response_model=TranscriptResponse)
async def get_audio(
    request: Request,
    start: float = Query(...),
    end: float = Query(...),
    include_words: bool = Query(default=False),
) -> TranscriptResponse:
    """Cross-session transcript segments overlapping ``[start, end]``.

    Per HELIOS.md §9.6 — returns ``TranscriptResponse`` with segments
    + a ``coverage`` object whose ``captured_seconds`` is the union
    length of segment intervals clamped to the requested window.
    """
    if end <= start:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "invalid_range",
            f"end ({end}) must be greater than start ({start})",
        )

    db = request.app.state.db_pool.writer
    segments = await queries.get_segments_in_range(db, start, end)
    captured = _captured_seconds(segments, start, end)
    return TranscriptResponse(
        session_id=None,
        started_at=start,
        ended_at=end,
        segments=[
            TranscriptSegmentResponse(
                start=s.start_ts,
                end=s.end_ts,
                speaker=s.speaker,
                text=s.text,
                words=_decode_words(s.words) if include_words else None,
            )
            for s in segments
        ],
        coverage=TranscriptCoverage(
            captured_seconds=captured,
            unavailable_ranges=[],
            transcription_pending_seconds=0.0,
        ),
        diarization_status="merged",
    )
