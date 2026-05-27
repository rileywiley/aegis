"""Meeting transcript builder + unattributed audio detector — Helios edition.

Helios returns pre-stitched transcript segments per session, so Aegis no longer
needs to assemble Screenpipe audio chunks. This module is now responsible for:

  * Calendar-domain logic — buffer padding, back-to-back midpoint truncation,
    overage detection (audio continues past scheduled end_time).
  * Translating Helios coverage_pct → Aegis ``transcript_status``.
  * Persisting the joined transcript text via the Meeting repository.

The legacy ``_stitch_transcript`` helper is gone — Helios serves pre-stitched
text. Wave 3J of the Helios integration deleted ``aegis/ingestion/screenpipe.py``.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.clients.helios import HeliosClient
from aegis.db import repositories
from aegis.db.models import Meeting
from aegis.ingestion.helios import get_transcript_for_meeting

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────
BUFFER_MINUTES = 5
MAX_OVERAGE_MINUTES = 30
BACK_TO_BACK_GAP_MINUTES = 5

# Coverage thresholds for transcript_status determination.
# coverage_pct is in [0.0, 1.0]; >= 0.8 = captured, > 0.0 = partial,
# 0.0 (or no transcript at all) = no_audio.
CAPTURED_COVERAGE_THRESHOLD = 0.8


class MeetingDetector:
    """Builds transcripts for completed meetings and detects unattributed audio.

    ``helios`` is the typed HTTP client for the Helios daemon; pass the
    instance from ``app.state.helios_client``. The detector itself is stateless,
    so a new instance per polling cycle is fine.
    """

    def __init__(self, helios: HeliosClient) -> None:
        self.helios = helios

    async def build_transcript(self, session: AsyncSession, meeting: Meeting) -> None:
        """Build a transcript for a completed meeting from Helios.

        Steps:
        1. Compute time window with 5-min buffer (before/after).
        2. Truncate buffer at the midpoint with back-to-back adjacent meetings.
        3. Ask Helios for transcript segments in the adjusted window.
        4. If audio extends past the scheduled end, query an overage extension
           up to MAX_OVERAGE_MINUTES and merge.
        5. Use coverage_pct to determine transcript_status; persist.
        """
        window_start = meeting.start_time - timedelta(minutes=BUFFER_MINUTES)
        window_end = meeting.end_time + timedelta(minutes=BUFFER_MINUTES)

        # Adjacent meeting truncation — calendar-domain logic, unrelated to
        # the capture backend.
        window_start, window_end = await self._adjust_for_adjacent(
            session, meeting, window_start, window_end
        )

        transcript = await get_transcript_for_meeting(
            self.helios, window_start, window_end
        )

        # Helios unreachable — treat as no_audio for now. The heartbeat loop
        # will surface the daemon-down state separately via system_health.
        if transcript is None:
            await repositories.update_meeting_transcript(
                session,
                meeting.id,
                transcript_text="",
                transcript_status="no_audio",
            )
            logger.info(
                "Meeting %d: helios unreachable, marking no_audio", meeting.id
            )
            return

        # Overage detection: if the latest segment runs past the scheduled
        # end, extend the window once and merge.
        latest_end_ts = max(
            (s.end_ts for s in transcript.segments), default=None
        )
        meeting_end_ts = meeting.end_time.timestamp()
        if latest_end_ts is not None and latest_end_ts > meeting_end_ts:
            overage_end = min(
                meeting.end_time + timedelta(minutes=MAX_OVERAGE_MINUTES),
                datetime.fromtimestamp(latest_end_ts, tz=timezone.utc)
                + timedelta(minutes=1),
            )
            if overage_end > window_end:
                extra = await get_transcript_for_meeting(
                    self.helios, window_end, overage_end
                )
                if extra is not None and extra.segments:
                    transcript.segments.extend(extra.segments)
                    # Recompute coverage against the extended duration.
                    extended_duration = max(
                        0.0, (overage_end - window_start).total_seconds()
                    )
                    if extended_duration > 0:
                        covered = sum(
                            max(0.0, s.end_ts - s.start_ts)
                            for s in transcript.segments
                        )
                        transcript.coverage_pct = min(
                            1.0, covered / extended_duration
                        )
                    logger.info(
                        "Meeting %d overage detected: extended to %s",
                        meeting.id,
                        overage_end.isoformat(),
                    )

        status = _status_from_coverage(transcript.coverage_pct, transcript.segments)
        await repositories.update_meeting_transcript(
            session,
            meeting.id,
            transcript_text=transcript.joined_text,
            transcript_status=status,
        )
        logger.info(
            "Meeting %d: transcript %s (%d segments, coverage=%.2f)",
            meeting.id,
            status,
            len(transcript.segments),
            transcript.coverage_pct,
        )

    async def detect_unattributed_audio(self, session: AsyncSession) -> list[dict]:
        """Scan for multi-speaker audio outside any calendar meeting window.

        Returns a list of dicts describing unattributed audio segments:
          {start, end, speaker_count, preview_text}
        """
        now = datetime.now(timezone.utc)
        scan_start = now - timedelta(hours=12)

        meetings = await repositories.get_meetings_for_range(session, scan_start, now)
        meeting_windows = [
            (
                m.start_time - timedelta(minutes=BUFFER_MINUTES),
                m.end_time + timedelta(minutes=BUFFER_MINUTES),
            )
            for m in meetings
        ]

        transcript = await get_transcript_for_meeting(self.helios, scan_start, now)
        if transcript is None or not transcript.segments:
            return []

        unattributed: list[dict] = []
        for seg in transcript.segments:
            seg_start = datetime.fromtimestamp(seg.start_ts, tz=timezone.utc)
            seg_end = datetime.fromtimestamp(seg.end_ts, tz=timezone.utc)
            inside_meeting = any(
                ws <= seg_start <= we for ws, we in meeting_windows
            )
            if inside_meeting:
                continue
            # Helios-tagged segments come pre-attributed; we only flag
            # multi-speaker activity here. Heuristic: at least one
            # SPEAKER_xx label on a non-mic segment.
            if not seg.speaker or seg.speaker == "user":
                continue
            text = (seg.text or "").strip()
            if not text:
                continue
            unattributed.append(
                {
                    "start": seg_start.isoformat(),
                    "end": seg_end.isoformat(),
                    "speaker_count": 1,
                    "preview_text": text[:200],
                }
            )

        return _merge_adjacent_segments(unattributed)

    async def process_completed_meetings(self, session: AsyncSession) -> int:
        """Find completed meetings with pending transcripts and build them.

        Returns the count of meetings processed.
        """
        stmt = (
            select(Meeting)
            .where(
                Meeting.end_time < datetime.now(timezone.utc),
                Meeting.transcript_status == "pending",
                Meeting.is_excluded.is_(False),
            )
            .order_by(Meeting.start_time)
        )
        result = await session.execute(stmt)
        meetings = list(result.scalars().all())

        count = 0
        for meeting in meetings:
            try:
                await self.build_transcript(session, meeting)
                count += 1
            except Exception:
                logger.exception(
                    "Failed to build transcript for meeting %d", meeting.id
                )
        return count

    async def _adjust_for_adjacent(
        self,
        session: AsyncSession,
        meeting: Meeting,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[datetime, datetime]:
        """Truncate padding if adjacent meetings sit within the gap threshold.

        For back-to-back meetings, use the midpoint between them as the
        boundary instead of overlapping buffer windows.
        """
        search_start = meeting.start_time - timedelta(minutes=BUFFER_MINUTES + 1)
        search_end = meeting.end_time + timedelta(minutes=BUFFER_MINUTES + 1)

        adjacent = await repositories.get_meetings_for_range(
            session, search_start, search_end
        )

        for adj in adjacent:
            if adj.id == meeting.id:
                continue

            # Previous meeting ends close to our start
            if adj.end_time <= meeting.start_time:
                gap = (meeting.start_time - adj.end_time).total_seconds() / 60
                if gap < BACK_TO_BACK_GAP_MINUTES:
                    midpoint = (
                        adj.end_time + (meeting.start_time - adj.end_time) / 2
                    )
                    if midpoint > window_start:
                        window_start = midpoint

            # Next meeting starts close to our end
            if adj.start_time >= meeting.end_time:
                gap = (adj.start_time - meeting.end_time).total_seconds() / 60
                if gap < BACK_TO_BACK_GAP_MINUTES:
                    midpoint = (
                        meeting.end_time + (adj.start_time - meeting.end_time) / 2
                    )
                    if midpoint < window_end:
                        window_end = midpoint

        return window_start, window_end


# ── Pure helpers ─────────────────────────────────────────


def _status_from_coverage(
    coverage_pct: float, segments: list
) -> str:
    """Map Helios coverage_pct → transcript_status.

    Thresholds:
      coverage_pct >= 0.8 → 'captured'
      0.0 <  coverage_pct <  0.8 → 'partial'
      coverage_pct == 0.0 (or empty segments) → 'no_audio'
    """
    if not segments or coverage_pct <= 0.0:
        return "no_audio"
    if coverage_pct >= CAPTURED_COVERAGE_THRESHOLD:
        return "captured"
    return "partial"


def _merge_adjacent_segments(segments: list[dict]) -> list[dict]:
    """Merge unattributed audio segments that are close together."""
    if not segments:
        return []

    sorted_segs = sorted(segments, key=lambda s: s["start"])
    merged: list[dict] = [sorted_segs[0]]

    for seg in sorted_segs[1:]:
        last = merged[-1]
        try:
            last_end = datetime.fromisoformat(last["end"])
            seg_start = datetime.fromisoformat(seg["start"])
            if (seg_start - last_end).total_seconds() < 300:
                last["end"] = seg["end"]
                last["speaker_count"] = max(
                    last["speaker_count"], seg["speaker_count"]
                )
                last["preview_text"] = last["preview_text"] or seg["preview_text"]
                continue
        except (ValueError, TypeError):
            pass
        merged.append(seg)

    return merged
