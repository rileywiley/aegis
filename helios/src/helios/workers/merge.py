"""Merge worker — assigns speakers to system-channel transcript segments.

Per HELIOS.md §9.4. Runs after BOTH transcription and diarization are
complete for a session. For every ``transcript_segments`` row in the
session whose ``speaker`` is NULL, finds the ``diarization_turns`` row
with the maximum temporal overlap and writes that turn's
``speaker_label`` as the segment's speaker. Mic-channel segments are
always set to ``speaker='user'`` by the transcription worker (Wave 2E)
so the merge worker skips them.

Strategy — max-overlap match:

* A single turn entirely covering a segment yields that speaker.
* Two turns split a segment → speaker with the most cumulative overlap.
* No turn intersects a segment → speaker stays NULL (rare — only when
  diarization missed a span that transcription covered, e.g. a very
  short utterance below the diarization model's threshold).
* Sessions with no diarization turns (disabled / not_applicable) are a
  no-op: mic segments already say ``user`` and system segments stay
  NULL.

Idempotency: the worker only updates segments whose ``speaker`` is
currently NULL, so re-running merge against the same session does not
overwrite an already-assigned speaker. This makes the
re-diarize → re-merge flow safe.

Logging contract: never log raw transcript text. Operational fields
(session_id, turn counts, segment counts, speaker labels like
``SPEAKER_00``) are fine.
"""

from __future__ import annotations

import asyncio

import aiosqlite

from helios.db import queries
from helios.db.rows import DiarizationTurnRow, TranscriptSegmentRow
from helios.log import get_logger

_log = get_logger("workers.merge")


class MergeWorker:
    """Background worker that assigns speakers to system-channel segments.

    Lifecycle mirrors the transcription / diarization workers:

    1. :meth:`start` spawns the consumer loop. Idempotent.
    2. :meth:`enqueue_session` is called by the diarization worker
       after it writes ``diarization_turns`` rows for a session.
    3. :meth:`stop` cancels the loop. Idempotent.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._loop_task: asyncio.Task | None = None
        self._stopping = False

    # ---------------------------------------------------------------- public

    async def start(self) -> None:
        """Spawn the queue-consumer loop. Idempotent."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="merge-worker"
        )

    async def stop(self) -> None:
        """Cancel the loop. Idempotent."""
        self._stopping = True
        task = self._loop_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._loop_task = None

    async def enqueue_session(self, session_id: int) -> None:
        """Enqueue a session for merge.

        Called by the diarization worker once ``diarization_turns`` rows
        have been written. Even if :meth:`start` hasn't been called yet
        the queue accepts the id and the loop will drain it on start.
        """
        await self._queue.put(session_id)

    # -------------------------------------------------------------- internal

    async def _run_loop(self) -> None:
        """Consume queued session ids and merge each in turn."""
        try:
            while not self._stopping:
                try:
                    session_id = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                if self._stopping:
                    return
                try:
                    await self._merge_session(session_id)
                except Exception as exc:  # noqa: BLE001 — never kill the loop
                    _log.warning(
                        "merge_session_failed",
                        session_id=session_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
        except asyncio.CancelledError:
            raise

    async def _merge_session(self, session_id: int) -> None:
        """Match diarization turns against transcript segments by max overlap."""
        turns = await queries.get_diarization_turns_for_session(
            self._db, session_id
        )
        if not turns:
            _log.info("merge_skip_no_turns", session_id=session_id)
            return

        # transcript_segments has no channel column; resolve via the
        # join to audio_chunks. The helper returns (segment, channel)
        # tuples ordered by segment start_ts.
        rows_with_channel = await queries.get_segments_with_channel_for_session(
            self._db, session_id
        )
        if not rows_with_channel:
            _log.info("merge_skip_no_segments", session_id=session_id)
            return

        merged_count = 0
        unmatched_count = 0
        for segment, channel in rows_with_channel:
            if channel == "mic":
                continue  # already 'user'
            if segment.speaker is not None:
                continue  # already assigned (idempotent re-run)
            best_speaker = _find_max_overlap_speaker(segment, turns)
            if best_speaker is not None:
                await queries.update_segment_speaker(
                    self._db, segment.id, best_speaker
                )
                merged_count += 1
            else:
                unmatched_count += 1
        _log.info(
            "merge_session_completed",
            session_id=session_id,
            merged=merged_count,
            unmatched=unmatched_count,
        )


def _find_max_overlap_speaker(
    segment: TranscriptSegmentRow, turns: list[DiarizationTurnRow]
) -> str | None:
    """Return the speaker_label whose turn(s) overlap ``segment`` the most.

    Computes ``overlap = max(0, min(end_a, end_b) - max(start_a, start_b))``
    for each turn, sums per-speaker (a single speaker may have multiple
    turns intersecting one segment), and returns the speaker with the
    largest total. Ties are broken by ``speaker_label`` sort order so
    repeated runs against the same data yield the same result.

    Returns ``None`` when no turn intersects the segment at all.
    """
    per_speaker: dict[str, float] = {}
    for turn in turns:
        overlap = min(segment.end_ts, turn.end_ts) - max(
            segment.start_ts, turn.start_ts
        )
        if overlap > 0:
            per_speaker[turn.speaker_label] = (
                per_speaker.get(turn.speaker_label, 0.0) + overlap
            )
    if not per_speaker:
        return None
    # Sort speakers ascending so ties resolve deterministically; max()
    # then returns the deterministically-first speaker among the
    # max-overlap set.
    return max(sorted(per_speaker), key=lambda k: per_speaker[k])


__all__ = ["MergeWorker"]
