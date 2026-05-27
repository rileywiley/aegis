"""Coverage computation for transcript responses.

Per HELIOS.md §9.5 ``GET /v1/sessions/{id}/transcript`` returns a
``coverage`` object alongside the segment list so callers can tell
whether the transcript is complete, in progress, or has gaps:

* ``captured_seconds`` — total audio successfully captured (chunks with
  ``status='recorded'`` or ``'transcribed'``, plus ``no_audio`` and
  ``transcription_failed`` since the audio existed but produced no
  segments).
* ``unavailable_ranges`` — time spans for chunks marked
  ``status='unavailable'`` (system_sleep, watchdog_stall, etc.). The
  reason is whatever was recorded in ``audio_chunks.unavailable_reason``.
* ``transcription_pending_seconds`` — chunks recorded but not yet
  transcribed (``status='recorded' AND transcribed_at IS NULL``).

The split between ``captured_seconds`` and ``transcription_pending_seconds``
matters for menu-bar UX: a session can show "30 minutes captured, 2
minutes still transcribing" without the user thinking 28 minutes are
missing.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from helios.db import queries


@dataclass
class CoverageRange:
    """One unavailable span in a session's audio coverage."""

    start_ts: float
    end_ts: float
    reason: str


@dataclass
class Coverage:
    """Aggregated coverage for one session, see module docstring."""

    captured_seconds: float
    unavailable_ranges: list[CoverageRange]
    transcription_pending_seconds: float


async def compute_session_coverage(
    db: aiosqlite.Connection,
    session_id: int,
    sample_rate: int = 16000,
) -> Coverage:
    """Compute :class:`Coverage` for a single session.

    ``sample_rate`` is the canonical audio sample rate from
    :class:`helios.config.AudioConfig` — used to convert
    ``audio_chunks.samples`` (an integer count) into seconds.
    """
    chunks = await queries.get_session_audio_chunks(db, session_id)
    captured = 0.0
    pending = 0.0
    unavailable: list[CoverageRange] = []
    for c in chunks:
        # samples / sample_rate gives the recorded duration in seconds.
        # Falling back to (end_ts - start_ts) when samples is 0 keeps
        # the math stable for 'unavailable' rows where samples may be 0.
        if c.samples > 0:
            duration = c.samples / float(sample_rate)
        else:
            duration = max(0.0, c.end_ts - c.start_ts)
        if c.status == "recorded":
            captured += duration
            if c.transcribed_at is None:
                pending += duration
        elif c.status == "transcribed":
            captured += duration
        elif c.status == "unavailable":
            unavailable.append(
                CoverageRange(
                    start_ts=c.start_ts,
                    end_ts=c.end_ts,
                    # ``reason`` is non-nullable on the wire; coerce
                    # any missing reason to a stable sentinel so the
                    # response still validates.
                    reason=c.unavailable_reason or "unknown",
                )
            )
        elif c.status == "no_audio":
            # Audio existed (the chunker wrote a silent buffer), it just
            # contained nothing transcribable. Counts as captured time.
            captured += duration
        elif c.status == "transcription_failed":
            # Audio was captured; transcription gave up after the max
            # attempts. Coverage-wise this is captured but unrecoverable.
            captured += duration
    return Coverage(
        captured_seconds=captured,
        unavailable_ranges=unavailable,
        transcription_pending_seconds=pending,
    )


__all__ = ["Coverage", "CoverageRange", "compute_session_coverage"]
