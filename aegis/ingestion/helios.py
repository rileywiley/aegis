"""High-level Helios ingestion helpers used by meeting_detector + poller.

Wraps the typed HeliosClient (``aegis/clients/helios.py``) with Aegis-domain
methods and small dataclasses that ``meeting_detector`` consumes.

Replaces the old ``aegis/ingestion/screenpipe.py`` — Phase 3 (Wave 3J)
deletes that file. Helios returns pre-stitched transcript segments per
session, so Aegis no longer needs the legacy chunk-stitching path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from aegis.clients.helios import HeliosClient

logger = logging.getLogger(__name__)


@dataclass
class HeliosTranscriptSegment:
    """One transcript segment returned by ``GET /v1/audio``."""

    start_ts: float
    end_ts: float
    text: str
    # 'user' for mic, SPEAKER_00..N for system audio diarization,
    # None when Helios returned no diarization for this segment.
    speaker: str | None
    words: list[dict] | None = None


@dataclass
class HeliosTranscript:
    """Wraps the result of a ``/v1/audio`` call — the segments that span
    a meeting window plus a derived coverage ratio.
    """

    segments: list[HeliosTranscriptSegment] = field(default_factory=list)
    # 0.0 to 1.0 — what fraction of the meeting window was transcribed.
    coverage_pct: float = 0.0

    @property
    def joined_text(self) -> str:
        """Concatenated transcript with speaker tags + newlines.

        Format used by downstream extraction prompts (Wave 4L):

            speaker_label: text
            speaker_label: text
            ...

        ``speaker_label`` is the raw Helios speaker (``user`` /
        ``SPEAKER_00`` / etc.) or ``?`` when no diarization is
        available. Speaker-name resolution to human names happens
        elsewhere (HELIOS.md §16.4) — this method intentionally
        keeps the raw labels so downstream callers can rewrite them.
        """
        out: list[str] = []
        for s in self.segments:
            label = (s.speaker or "?").strip() or "?"
            text = s.text.strip()
            if not text:
                continue
            out.append(f"{label}: {text}")
        return "\n".join(out)


async def get_transcript_for_meeting(
    client: HeliosClient,
    start: datetime,
    end: datetime,
    *,
    include_words: bool = False,
) -> HeliosTranscript | None:
    """Fetch transcript segments covering ``[start, end]``.

    Returns ``None`` if Helios is unreachable (the underlying
    ``HeliosClient`` returns ``None`` on connect / timeout / 401-after-
    refresh / non-2xx). Returns an empty ``HeliosTranscript`` (with
    ``coverage_pct = 0.0``) if Helios responded but had no segments.
    """
    raw = await client.get_transcript_for_meeting(
        start, end, include_words=include_words
    )
    if raw is None:
        return None

    segments_raw = raw.get("segments", []) or []
    segments: list[HeliosTranscriptSegment] = []
    for s in segments_raw:
        try:
            segments.append(
                HeliosTranscriptSegment(
                    # Helios serializes ``TranscriptSegmentResponse`` with
                    # ``start`` / ``end`` keys (HELIOS.md §7.4 — see
                    # ``helios.api.schemas.TranscriptSegmentResponse``).
                    # Aegis stores them as ``start_ts`` / ``end_ts`` to
                    # disambiguate from datetime ``start``. Lock the
                    # contract via ``test_segment_contract_matches_helios_schema``.
                    start_ts=float(s["start"]),
                    end_ts=float(s["end"]),
                    text=s.get("text", "") or "",
                    speaker=s.get("speaker"),
                    words=s.get("words") if include_words else None,
                )
            )
        except (KeyError, TypeError, ValueError):
            # Malformed segment — drop it and keep going.
            logger.debug("helios_segment_malformed_skipped")
            continue

    meeting_duration = max(0.0, (end - start).total_seconds())
    if meeting_duration <= 0 or not segments:
        coverage_pct = 0.0
    else:
        covered = sum(max(0.0, s.end_ts - s.start_ts) for s in segments)
        coverage_pct = min(1.0, covered / meeting_duration)

    return HeliosTranscript(segments=segments, coverage_pct=coverage_pct)


async def get_ocr_for_meeting(
    client: HeliosClient, start: datetime, end: datetime
) -> dict | None:
    """OCR frames spanning a meeting window.

    Phase 5 makes this real; for now this is a passthrough so callers
    can be wired up without depending on Phase-5 work.
    """
    return await client.get_ocr(start, end)


async def health_check(client: HeliosClient) -> bool:
    """Convenience pass-through for callers that don't need anything else."""
    return await client.health_check()
