"""Meeting transcript builder and unattributed audio detector.

Queries Screenpipe for audio after calendar meetings end, stitches
transcripts, handles back-to-back meetings and overage detection.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db import repositories
from aegis.db.models import Meeting
from aegis.ingestion.screenpipe import ScreenpipeClient

logger = logging.getLogger(__name__)

# Constants
BUFFER_MINUTES = 5
MAX_OVERAGE_MINUTES = 30
BACK_TO_BACK_GAP_MINUTES = 5


class MeetingDetector:
    """Builds transcripts for completed meetings and detects unattributed audio."""

    def __init__(self, screenpipe: ScreenpipeClient | None = None) -> None:
        self.screenpipe = screenpipe or ScreenpipeClient()

    async def build_transcript(self, session: AsyncSession, meeting: Meeting) -> None:
        """Build a transcript for a completed meeting from Screenpipe audio.

        Steps:
        1. Calculate time window with 5-min buffer (before/after)
        2. Detect adjacent meetings and truncate padding to avoid overlap
        3. Query Screenpipe for audio in the adjusted window
        4. Detect overage (audio continues past scheduled end)
        5. Stitch chunks into transcript, set status, persist
        """
        window_start = meeting.start_time - timedelta(minutes=BUFFER_MINUTES)
        window_end = meeting.end_time + timedelta(minutes=BUFFER_MINUTES)

        # Detect adjacent meetings and truncate padding
        window_start, window_end = await self._adjust_for_adjacent(
            session, meeting, window_start, window_end
        )

        # Query Screenpipe for audio in the window
        audio_chunks = await self.screenpipe.get_audio(window_start, window_end)

        if not audio_chunks:
            # No audio at all — mark as no_audio
            await repositories.update_meeting_transcript(
                session, meeting.id, transcript_text="", transcript_status="no_audio"
            )
            logger.info("Meeting %d (%s): no audio found", meeting.id, meeting.title)
            return

        # Overage detection: check for audio past meeting.end_time
        latest_chunk_time = _latest_timestamp(audio_chunks)
        if latest_chunk_time and latest_chunk_time > meeting.end_time:
            # Audio continues past scheduled end — extend window up to 30 min
            overage_end = min(
                meeting.end_time + timedelta(minutes=MAX_OVERAGE_MINUTES),
                latest_chunk_time + timedelta(minutes=1),
            )
            if overage_end > window_end:
                extra_chunks = await self.screenpipe.get_audio(window_end, overage_end)
                audio_chunks.extend(extra_chunks)
                logger.info(
                    "Meeting %d overage detected: audio continues to %s",
                    meeting.id,
                    overage_end.isoformat(),
                )

        # Stitch chunks into a single transcript
        transcript = _stitch_transcript(audio_chunks)

        # Determine transcript status
        status = _determine_status(audio_chunks, meeting.start_time, meeting.end_time)

        await repositories.update_meeting_transcript(
            session, meeting.id, transcript_text=transcript, transcript_status=status
        )
        logger.info(
            "Meeting %d (%s): transcript %s (%d chunks)",
            meeting.id,
            meeting.title,
            status,
            len(audio_chunks),
        )

    async def detect_unattributed_audio(self, session: AsyncSession) -> list[dict]:
        """Scan for multi-speaker audio outside any calendar meeting window.

        Returns a list of dicts describing unattributed audio segments:
          {start, end, speaker_count, preview_text}
        """
        now = datetime.now(timezone.utc)
        scan_start = now - timedelta(hours=12)

        # Get all meetings in the scan window
        meetings = await repositories.get_meetings_for_range(session, scan_start, now)
        meeting_windows = [
            (m.start_time - timedelta(minutes=BUFFER_MINUTES),
             m.end_time + timedelta(minutes=BUFFER_MINUTES))
            for m in meetings
        ]

        # Get all audio in the scan window
        audio_chunks = await self.screenpipe.get_audio(scan_start, now)
        if not audio_chunks:
            return []

        # Filter out chunks that fall inside a meeting window
        unattributed: list[dict] = []
        for chunk in audio_chunks:
            chunk_time = _parse_chunk_timestamp(chunk)
            if chunk_time is None:
                continue

            # Check if this chunk is inside any meeting window
            inside_meeting = any(
                ws <= chunk_time <= we for ws, we in meeting_windows
            )
            if inside_meeting:
                continue

            # Check for multi-speaker (at least hints of conversation)
            speakers = _get_speakers(chunk)
            if len(speakers) < 2:
                continue

            text = _get_chunk_text(chunk)
            unattributed.append({
                "start": chunk_time.isoformat(),
                "end": chunk_time.isoformat(),
                "speaker_count": len(speakers),
                "preview_text": text[:200] if text else "",
            })

        # Merge adjacent segments
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
                logger.exception("Failed to build transcript for meeting %d", meeting.id)
        return count

    async def _adjust_for_adjacent(
        self,
        session: AsyncSession,
        meeting: Meeting,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[datetime, datetime]:
        """Truncate padding if adjacent meetings are within BACK_TO_BACK_GAP_MINUTES.

        For back-to-back meetings, use the midpoint between them as the boundary
        instead of overlapping buffer windows.
        """
        # Look for meetings shortly before this one
        search_start = meeting.start_time - timedelta(minutes=BUFFER_MINUTES + 1)
        search_end = meeting.end_time + timedelta(minutes=BUFFER_MINUTES + 1)

        adjacent = await repositories.get_meetings_for_range(session, search_start, search_end)

        for adj in adjacent:
            if adj.id == meeting.id:
                continue

            # Previous meeting ends close to our start
            if adj.end_time <= meeting.start_time:
                gap = (meeting.start_time - adj.end_time).total_seconds() / 60
                if gap < BACK_TO_BACK_GAP_MINUTES:
                    midpoint = adj.end_time + (meeting.start_time - adj.end_time) / 2
                    if midpoint > window_start:
                        window_start = midpoint

            # Next meeting starts close to our end
            if adj.start_time >= meeting.end_time:
                gap = (adj.start_time - meeting.end_time).total_seconds() / 60
                if gap < BACK_TO_BACK_GAP_MINUTES:
                    midpoint = meeting.end_time + (adj.start_time - meeting.end_time) / 2
                    if midpoint < window_end:
                        window_end = midpoint

        return window_start, window_end


def _stitch_transcript(chunks: list[dict]) -> str:
    """Stitch audio chunks into a single transcript with speaker labels."""
    lines: list[str] = []
    for chunk in sorted(chunks, key=lambda c: _get_chunk_sort_key(c)):
        content = chunk.get("content", chunk)
        speaker = content.get("speaker", {})
        # Speaker can be a dict with "id" or a simple string
        if isinstance(speaker, dict):
            speaker_label = speaker.get("name") or speaker.get("id") or "Unknown"
        elif isinstance(speaker, str) and speaker:
            speaker_label = speaker
        else:
            speaker_label = "Speaker"

        text = _get_chunk_text(chunk)
        if text:
            lines.append(f"[{speaker_label}]: {text}")

    return "\n".join(lines)


def _determine_status(
    chunks: list[dict], start_time: datetime, end_time: datetime
) -> str:
    """Determine transcript status based on audio coverage.

    - 'captured': good coverage across the meeting duration
    - 'partial': some audio but gaps detected
    """
    if not chunks:
        return "no_audio"

    # Check for coverage: if we have chunks spanning at least 50% of the meeting
    duration_minutes = (end_time - start_time).total_seconds() / 60
    if duration_minutes <= 0:
        return "captured"

    timestamps = [_parse_chunk_timestamp(c) for c in chunks]
    valid_ts = [t for t in timestamps if t is not None]
    if not valid_ts:
        return "partial"

    earliest = min(valid_ts)
    latest = max(valid_ts)
    coverage = (latest - earliest).total_seconds() / 60

    # If coverage is at least 50% of meeting duration, consider it captured
    if coverage >= duration_minutes * 0.5:
        return "captured"
    return "partial"


def _get_chunk_text(chunk: dict) -> str:
    """Extract text from a Screenpipe audio chunk."""
    content = chunk.get("content", chunk)
    return content.get("text", "").strip()


def _get_speakers(chunk: dict) -> set[str]:
    """Extract unique speaker identifiers from a chunk."""
    content = chunk.get("content", chunk)
    speaker = content.get("speaker", {})
    speakers: set[str] = set()
    if isinstance(speaker, dict):
        sid = speaker.get("name") or speaker.get("id")
        if sid:
            speakers.add(str(sid))
    elif isinstance(speaker, str) and speaker:
        speakers.add(speaker)
    # Some Screenpipe responses include a speakers list
    for s in content.get("speakers", []):
        if isinstance(s, dict):
            sid = s.get("name") or s.get("id")
            if sid:
                speakers.add(str(sid))
        elif isinstance(s, str) and s:
            speakers.add(s)
    return speakers


def _parse_chunk_timestamp(chunk: dict) -> datetime | None:
    """Parse the timestamp from a Screenpipe chunk."""
    content = chunk.get("content", chunk)
    ts_str = content.get("timestamp")
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _latest_timestamp(chunks: list[dict]) -> datetime | None:
    """Get the latest timestamp from a list of audio chunks."""
    timestamps = [_parse_chunk_timestamp(c) for c in chunks]
    valid = [t for t in timestamps if t is not None]
    return max(valid) if valid else None


def _get_chunk_sort_key(chunk: dict) -> str:
    """Sort key for ordering chunks by timestamp."""
    content = chunk.get("content", chunk)
    return content.get("timestamp", "")


def _merge_adjacent_segments(segments: list[dict]) -> list[dict]:
    """Merge unattributed audio segments that are close together."""
    if not segments:
        return []

    # Sort by start time
    sorted_segs = sorted(segments, key=lambda s: s["start"])
    merged: list[dict] = [sorted_segs[0]]

    for seg in sorted_segs[1:]:
        last = merged[-1]
        # If within 5 minutes of the last segment, merge
        try:
            last_end = datetime.fromisoformat(last["end"])
            seg_start = datetime.fromisoformat(seg["start"])
            if (seg_start - last_end).total_seconds() < 300:
                last["end"] = seg["end"]
                last["speaker_count"] = max(last["speaker_count"], seg["speaker_count"])
                last["preview_text"] = last["preview_text"] or seg["preview_text"]
                continue
        except (ValueError, TypeError):
            pass
        merged.append(seg)

    return merged
