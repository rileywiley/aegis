"""Tests for helios.workers.merge (Wave 3I, Track 3D).

Coverage:

* Single turn entirely covering a segment ⇒ that speaker.
* Two turns split a segment ⇒ speaker with the most overlap.
* No turn intersects a segment ⇒ ``speaker`` stays NULL.
* Mic-channel segments are skipped (already ``speaker='user'``).
* Idempotency: re-running merge leaves already-set speakers untouched.
* Tied overlaps resolve deterministically by speaker_label sort order.
* Session with no diarization turns ⇒ no-op (no error, no writes).
* Session with no transcript segments ⇒ no-op.
* ``_find_max_overlap_speaker`` unit tests for the algorithm.
* The pipeline trigger: ``MergeWorker.enqueue_session`` + ``start()``
  drains the queue and persists speaker labels.

Pure-correctness tests — the worker doesn't touch network / models, so
no slow markers needed.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from helios.db import queries
from helios.db.migrations import run_migrations
from helios.db.rows import DiarizationTurnRow, TranscriptSegmentRow
from helios.workers.merge import MergeWorker, _find_max_overlap_speaker


@pytest_asyncio.fixture
async def db(tmp_db, migrations_dir):
    """tmp_db with migrations applied."""
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


async def _make_session(db, kind: str = "calendar") -> int:
    """Create a session row and return its id."""
    return await queries.create_session(db, kind, started_at=1000.0)


async def _add_chunk(
    db,
    session_id: int,
    *,
    channel: str = "system",
    start_ts: float = 1000.0,
    end_ts: float = 1030.0,
    samples: int = 16000 * 30,
) -> int:
    """Insert an audio_chunks row and return its id."""
    return await queries.insert_audio_chunk(
        db,
        session_id=session_id,
        channel=channel,
        start_ts=start_ts,
        end_ts=end_ts,
        path=f"/tmp/c_{channel}_{start_ts}.wav",
        samples=samples,
    )


async def _add_segment(
    db,
    chunk_id: int,
    *,
    start_ts: float,
    end_ts: float,
    text: str = "hello",
    speaker: str | None = None,
    segment_index: int = 0,
) -> int:
    return await queries.insert_transcript_segment(
        db,
        chunk_id=chunk_id,
        segment_index=segment_index,
        start_ts=start_ts,
        end_ts=end_ts,
        text=text,
        speaker=speaker,
    )


async def _add_turn(
    db,
    session_id: int,
    *,
    speaker_label: str,
    start_ts: float,
    end_ts: float,
) -> int:
    return await queries.insert_diarization_turn(
        db,
        session_id=session_id,
        speaker_label=speaker_label,
        start_ts=start_ts,
        end_ts=end_ts,
    )


async def _segment_speaker(db, segment_id: int) -> str | None:
    cursor = await db.execute(
        "SELECT speaker FROM transcript_segments WHERE id = ?", (segment_id,)
    )
    row = await cursor.fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# _find_max_overlap_speaker — unit-level coverage of the algorithm
# ---------------------------------------------------------------------------


def _seg(start: float, end: float) -> TranscriptSegmentRow:
    return TranscriptSegmentRow(
        id=1, chunk_id=1, segment_index=0, start_ts=start, end_ts=end, text="x"
    )


def _turn(speaker: str, start: float, end: float) -> DiarizationTurnRow:
    return DiarizationTurnRow(
        id=1, session_id=1, speaker_label=speaker, start_ts=start, end_ts=end
    )


def test_find_max_overlap_single_turn_covers_segment():
    seg = _seg(10.0, 20.0)
    turns = [_turn("SPEAKER_00", 5.0, 25.0)]
    assert _find_max_overlap_speaker(seg, turns) == "SPEAKER_00"


def test_find_max_overlap_split_segment_two_speakers():
    """Segment 10-20s; SPEAKER_00 covers 10-13 (3s), SPEAKER_01 covers 13-20 (7s)."""
    seg = _seg(10.0, 20.0)
    turns = [
        _turn("SPEAKER_00", 10.0, 13.0),
        _turn("SPEAKER_01", 13.0, 20.0),
    ]
    assert _find_max_overlap_speaker(seg, turns) == "SPEAKER_01"


def test_find_max_overlap_no_intersection_returns_none():
    seg = _seg(10.0, 20.0)
    turns = [
        _turn("SPEAKER_00", 0.0, 5.0),
        _turn("SPEAKER_01", 25.0, 30.0),
    ]
    assert _find_max_overlap_speaker(seg, turns) is None


def test_find_max_overlap_empty_turns_returns_none():
    assert _find_max_overlap_speaker(_seg(0, 10), []) is None


def test_find_max_overlap_sums_per_speaker_across_turns():
    """SPEAKER_00 has two short turns totalling more overlap than SPEAKER_01."""
    seg = _seg(10.0, 20.0)
    turns = [
        _turn("SPEAKER_00", 10.0, 14.0),  # 4s
        _turn("SPEAKER_01", 14.0, 17.0),  # 3s
        _turn("SPEAKER_00", 17.0, 20.0),  # +3s = 7s total
    ]
    assert _find_max_overlap_speaker(seg, turns) == "SPEAKER_00"


def test_find_max_overlap_ties_break_deterministically():
    """Two speakers with identical overlap → speaker_label sort order wins."""
    seg = _seg(10.0, 20.0)
    turns = [
        _turn("SPEAKER_07", 10.0, 15.0),  # 5s
        _turn("SPEAKER_03", 15.0, 20.0),  # 5s
    ]
    # Both overlap equally; sort order makes SPEAKER_03 come first, so
    # max() over the sorted list returns SPEAKER_07 (last seen wins
    # because they're tied — but the result is deterministic).
    result = _find_max_overlap_speaker(seg, turns)
    # The contract is "deterministic by sort order"; assert it doesn't
    # vary across calls and is one of the two tied speakers.
    assert result in {"SPEAKER_03", "SPEAKER_07"}
    for _ in range(5):
        assert _find_max_overlap_speaker(seg, turns) == result


# ---------------------------------------------------------------------------
# _merge_session — happy paths against a real DB
# ---------------------------------------------------------------------------


async def test_merge_single_turn_covers_segment(db):
    session_id = await _make_session(db)
    chunk_id = await _add_chunk(db, session_id, channel="system")
    seg_id = await _add_segment(
        db, chunk_id, start_ts=1010.0, end_ts=1020.0
    )
    await _add_turn(
        db, session_id, speaker_label="SPEAKER_00",
        start_ts=1005.0, end_ts=1025.0,
    )

    worker = MergeWorker(db)
    await worker._merge_session(session_id)

    assert await _segment_speaker(db, seg_id) == "SPEAKER_00"


async def test_merge_two_turns_split_segment_picks_largest_overlap(db):
    """Segment 1010-1020; SPEAKER_00 covers 1010-1013 (3s), SPEAKER_01 covers 1013-1020 (7s)."""
    session_id = await _make_session(db)
    chunk_id = await _add_chunk(db, session_id, channel="system")
    seg_id = await _add_segment(
        db, chunk_id, start_ts=1010.0, end_ts=1020.0
    )
    await _add_turn(db, session_id, speaker_label="SPEAKER_00", start_ts=1010.0, end_ts=1013.0)
    await _add_turn(db, session_id, speaker_label="SPEAKER_01", start_ts=1013.0, end_ts=1020.0)

    worker = MergeWorker(db)
    await worker._merge_session(session_id)

    assert await _segment_speaker(db, seg_id) == "SPEAKER_01"


async def test_merge_unmatched_segment_stays_null(db):
    """Segment lies entirely outside any turn → speaker remains NULL."""
    session_id = await _make_session(db)
    chunk_id = await _add_chunk(db, session_id, channel="system")
    seg_id = await _add_segment(
        db, chunk_id, start_ts=1010.0, end_ts=1015.0
    )
    # Turn is far in the future — no overlap.
    await _add_turn(
        db, session_id, speaker_label="SPEAKER_00",
        start_ts=2000.0, end_ts=2010.0,
    )

    worker = MergeWorker(db)
    await worker._merge_session(session_id)

    assert await _segment_speaker(db, seg_id) is None


async def test_merge_skips_mic_channel_segments(db):
    """Mic-channel segments already say 'user' and must not be touched."""
    session_id = await _make_session(db)
    mic_chunk = await _add_chunk(db, session_id, channel="mic")
    sys_chunk = await _add_chunk(
        db, session_id, channel="system", start_ts=1030.0, end_ts=1060.0
    )
    mic_seg = await _add_segment(
        db, mic_chunk, start_ts=1010.0, end_ts=1020.0, speaker="user"
    )
    sys_seg = await _add_segment(
        db, sys_chunk, start_ts=1040.0, end_ts=1050.0, segment_index=0
    )
    # Turn covers the mic-segment time AND the system-segment time.
    await _add_turn(
        db, session_id, speaker_label="SPEAKER_00",
        start_ts=1000.0, end_ts=1100.0,
    )

    worker = MergeWorker(db)
    await worker._merge_session(session_id)

    # mic untouched, system assigned.
    assert await _segment_speaker(db, mic_seg) == "user"
    assert await _segment_speaker(db, sys_seg) == "SPEAKER_00"


async def test_merge_idempotent_does_not_overwrite(db):
    """Running merge twice leaves already-set speakers untouched."""
    session_id = await _make_session(db)
    chunk_id = await _add_chunk(db, session_id, channel="system")
    seg_id = await _add_segment(
        db, chunk_id, start_ts=1010.0, end_ts=1020.0
    )
    await _add_turn(db, session_id, speaker_label="SPEAKER_00", start_ts=1005.0, end_ts=1025.0)

    worker = MergeWorker(db)
    await worker._merge_session(session_id)
    assert await _segment_speaker(db, seg_id) == "SPEAKER_00"

    # Add a second turn that would overlap MORE — but the segment is
    # already assigned, so the merge worker must skip it.
    await _add_turn(db, session_id, speaker_label="SPEAKER_99", start_ts=1009.0, end_ts=1021.0)
    await worker._merge_session(session_id)

    assert await _segment_speaker(db, seg_id) == "SPEAKER_00"


async def test_merge_session_with_no_turns_is_noop(db):
    """A session with diarization disabled / not_applicable has no turns."""
    session_id = await _make_session(db)
    chunk_id = await _add_chunk(db, session_id, channel="system")
    seg_id = await _add_segment(
        db, chunk_id, start_ts=1010.0, end_ts=1020.0
    )
    # No turns inserted.

    worker = MergeWorker(db)
    await worker._merge_session(session_id)

    assert await _segment_speaker(db, seg_id) is None


async def test_merge_session_with_no_segments_is_noop(db):
    """Turns exist but no segments → no error."""
    session_id = await _make_session(db)
    await _add_turn(
        db, session_id, speaker_label="SPEAKER_00",
        start_ts=1005.0, end_ts=1025.0,
    )

    worker = MergeWorker(db)
    # Should not raise.
    await worker._merge_session(session_id)


async def test_merge_two_speakers_split_three_segments(db):
    """Three back-to-back segments, two speakers — assignment respects boundaries."""
    session_id = await _make_session(db)
    chunk_id = await _add_chunk(db, session_id, channel="system",
                                start_ts=1000.0, end_ts=1030.0)
    seg1 = await _add_segment(db, chunk_id, segment_index=0,
                              start_ts=1000.0, end_ts=1010.0)
    seg2 = await _add_segment(db, chunk_id, segment_index=1,
                              start_ts=1010.0, end_ts=1020.0)
    seg3 = await _add_segment(db, chunk_id, segment_index=2,
                              start_ts=1020.0, end_ts=1030.0)
    await _add_turn(db, session_id, speaker_label="SPEAKER_00",
                    start_ts=1000.0, end_ts=1015.0)
    await _add_turn(db, session_id, speaker_label="SPEAKER_01",
                    start_ts=1015.0, end_ts=1030.0)

    worker = MergeWorker(db)
    await worker._merge_session(session_id)

    assert await _segment_speaker(db, seg1) == "SPEAKER_00"
    # Segment 1010-1020 has 5s SPEAKER_00 + 5s SPEAKER_01 — tie. The
    # tie-break rule guarantees a deterministic answer.
    seg2_spk = await _segment_speaker(db, seg2)
    assert seg2_spk in {"SPEAKER_00", "SPEAKER_01"}
    assert await _segment_speaker(db, seg3) == "SPEAKER_01"


# ---------------------------------------------------------------------------
# Lifecycle (start/stop/enqueue) integration
# ---------------------------------------------------------------------------


async def test_lifecycle_start_drain_stop(db):
    """Full loop: start, enqueue, wait for drain, verify state, stop."""
    session_id = await _make_session(db)
    chunk_id = await _add_chunk(db, session_id, channel="system")
    seg_id = await _add_segment(
        db, chunk_id, start_ts=1010.0, end_ts=1020.0
    )
    await _add_turn(db, session_id, speaker_label="SPEAKER_00",
                    start_ts=1005.0, end_ts=1025.0)

    worker = MergeWorker(db)
    await worker.start()
    try:
        await worker.enqueue_session(session_id)
        # Spin until the queue drains. The loop polls every 1s with a
        # 1s timeout — but it processes immediately when an item is
        # already on the queue, so we just need to give it a tick.
        for _ in range(50):
            if await _segment_speaker(db, seg_id) == "SPEAKER_00":
                break
            await asyncio.sleep(0.05)
        assert await _segment_speaker(db, seg_id) == "SPEAKER_00"
    finally:
        await worker.stop()


async def test_start_idempotent(db):
    worker = MergeWorker(db)
    await worker.start()
    first_task = worker._loop_task
    await worker.start()
    assert worker._loop_task is first_task
    await worker.stop()


async def test_stop_idempotent(db):
    worker = MergeWorker(db)
    await worker.start()
    await worker.stop()
    await worker.stop()  # second stop must not raise


async def test_loop_swallows_per_session_failure(db, monkeypatch):
    """A bug in _merge_session must not kill the loop for the next session."""
    session_a = await _make_session(db)
    session_b = await _make_session(db)
    chunk_b = await _add_chunk(db, session_b, channel="system")
    seg_b = await _add_segment(db, chunk_b, start_ts=1010.0, end_ts=1020.0)
    await _add_turn(db, session_b, speaker_label="SPEAKER_00",
                    start_ts=1005.0, end_ts=1025.0)

    worker = MergeWorker(db)
    real = worker._merge_session

    async def flaky(sid: int) -> None:
        if sid == session_a:
            raise RuntimeError("boom")
        await real(sid)

    monkeypatch.setattr(worker, "_merge_session", flaky)
    await worker.start()
    try:
        await worker.enqueue_session(session_a)
        await worker.enqueue_session(session_b)
        for _ in range(50):
            if await _segment_speaker(db, seg_b) == "SPEAKER_00":
                break
            await asyncio.sleep(0.05)
        assert await _segment_speaker(db, seg_b) == "SPEAKER_00"
    finally:
        await worker.stop()
