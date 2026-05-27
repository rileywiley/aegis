"""Tests for helios.db.queries — Wave 2C.

Uses the tmp_db fixture (raw aiosqlite connection with FK enabled) and
applies migrations on top so the queries hit a real schema.
"""

import time

import aiosqlite
import pytest
import pytest_asyncio

from helios.db.migrations import run_migrations
from helios.db.queries import (
    create_session,
    end_session,
    get_active_session,
    get_chunk_by_id,
    get_chunks_in_range,
    get_component_status,
    get_pending_chunks,
    get_session_audio_chunks,
    get_sessions_for_calendar_event,
    get_sessions_overlapping,
    insert_audio_chunk,
    insert_session_calendar_link,
    log_daemon_event,
    mark_chunk_archived,
    mark_chunk_transcribed,
    mark_chunk_transcription_failed,
    mark_chunk_unavailable,
    update_session_ended,
    upsert_component_status,
)
from helios.db.rows import AudioChunkRow, CaptureSessionRow, ComponentStatusRow


@pytest_asyncio.fixture
async def db(tmp_db, migrations_dir):
    """tmp_db with migrations applied."""
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


# ─────────────────────────────────────────────────────────────────────
# Audio chunk queries
# ─────────────────────────────────────────────────────────────────────


async def test_get_chunk_by_id_round_trip(db):
    session_id = await create_session(db, "manual_screen", started_at=1000.0)
    chunk_id = await insert_audio_chunk(
        db,
        session_id=session_id,
        channel="mic",
        start_ts=1000.0,
        end_ts=1030.0,
        path="/tmp/x.wav",
        samples=16000 * 30,
        partial=False,
    )
    chunk = await get_chunk_by_id(db, chunk_id)
    assert chunk is not None
    assert isinstance(chunk, AudioChunkRow)
    assert chunk.id == chunk_id
    assert chunk.session_id == session_id
    assert chunk.channel == "mic"
    assert chunk.start_ts == 1000.0
    assert chunk.end_ts == 1030.0
    assert chunk.path == "/tmp/x.wav"
    assert chunk.samples == 16000 * 30
    assert chunk.partial is False
    assert chunk.status == "recorded"
    assert chunk.transcribed_at is None
    assert chunk.unavailable_reason is None


async def test_get_chunk_by_id_missing_returns_none(db):
    assert await get_chunk_by_id(db, 99999) is None


async def test_get_pending_chunks_filters_status_and_transcribed_at(db):
    session_id = await create_session(db, "continuous", started_at=1000.0)

    # 1) recorded + pending  → should be returned
    pending_id = await insert_audio_chunk(
        db, session_id, "mic", 1000.0, 1030.0, "/tmp/a.wav", 16000 * 30
    )
    # 2) recorded but already transcribed → excluded
    transcribed_id = await insert_audio_chunk(
        db, session_id, "mic", 1030.0, 1060.0, "/tmp/b.wav", 16000 * 30
    )
    await mark_chunk_transcribed(db, transcribed_id, transcribed_at=1100.0)
    # 3) no_audio status → excluded
    no_audio_id = await insert_audio_chunk(
        db, session_id, "mic", 1060.0, 1090.0, None, 0
    )
    await mark_chunk_unavailable(db, no_audio_id, "silence")

    pending = await get_pending_chunks(db)
    ids = [c.id for c in pending]
    assert ids == [pending_id]


async def test_get_pending_chunks_ordered_by_start_ts_and_limit(db):
    session_id = await create_session(db, "continuous", started_at=1000.0)
    # Insert out of order
    c2 = await insert_audio_chunk(db, session_id, "mic", 2000.0, 2030.0, "/p2", 1)
    c1 = await insert_audio_chunk(db, session_id, "mic", 1000.0, 1030.0, "/p1", 1)
    c3 = await insert_audio_chunk(db, session_id, "mic", 3000.0, 3030.0, "/p3", 1)

    pending = await get_pending_chunks(db, limit=2)
    assert [c.id for c in pending] == [c1, c2]

    all_pending = await get_pending_chunks(db, limit=10)
    assert [c.id for c in all_pending] == [c1, c2, c3]


async def test_get_session_audio_chunks_ordered(db):
    session_id = await create_session(db, "continuous", started_at=1000.0)
    other_session = await create_session(db, "continuous", started_at=5000.0)

    # Insert out of order
    mid = await insert_audio_chunk(db, session_id, "mic", 1500.0, 1530.0, "/m", 1)
    early = await insert_audio_chunk(db, session_id, "mic", 1000.0, 1030.0, "/e", 1)
    late = await insert_audio_chunk(db, session_id, "mic", 2000.0, 2030.0, "/l", 1)
    # A chunk in another session — must not appear
    await insert_audio_chunk(db, other_session, "mic", 1200.0, 1230.0, "/o", 1)

    chunks = await get_session_audio_chunks(db, session_id)
    assert [c.id for c in chunks] == [early, mid, late]


async def test_get_chunks_in_range(db):
    session_id = await create_session(db, "continuous", started_at=0.0)
    # Window: [1000, 2000)
    before = await insert_audio_chunk(db, session_id, "mic", 500.0, 900.0, "/before", 1)
    overlap_start = await insert_audio_chunk(
        db, session_id, "mic", 950.0, 1100.0, "/os", 1
    )
    fully_inside = await insert_audio_chunk(
        db, session_id, "mic", 1200.0, 1500.0, "/inside", 1
    )
    overlap_end = await insert_audio_chunk(
        db, session_id, "mic", 1900.0, 2100.0, "/oe", 1
    )
    after = await insert_audio_chunk(db, session_id, "mic", 2500.0, 2600.0, "/after", 1)
    # Chunk that exactly touches at end_ts == range_start (no overlap)
    touching = await insert_audio_chunk(
        db, session_id, "mic", 800.0, 1000.0, "/touch", 1
    )

    chunks = await get_chunks_in_range(db, 1000.0, 2000.0)
    ids = {c.id for c in chunks}
    assert ids == {overlap_start, fully_inside, overlap_end}
    assert before not in ids
    assert after not in ids
    assert touching not in ids  # end_ts == range_start, strict overlap


async def test_mark_chunk_transcribed_round_trip(db):
    session_id = await create_session(db, "continuous", started_at=0.0)
    cid = await insert_audio_chunk(db, session_id, "mic", 0.0, 30.0, "/p", 1)
    await mark_chunk_transcribed(db, cid, transcribed_at=12345.0)
    chunk = await get_chunk_by_id(db, cid)
    assert chunk.status == "transcribed"
    assert chunk.transcribed_at == 12345.0


async def test_mark_chunk_transcribed_default_now(db):
    session_id = await create_session(db, "continuous", started_at=0.0)
    cid = await insert_audio_chunk(db, session_id, "mic", 0.0, 30.0, "/p", 1)
    before = time.time()
    await mark_chunk_transcribed(db, cid)
    after = time.time()
    chunk = await get_chunk_by_id(db, cid)
    assert chunk.status == "transcribed"
    assert before <= chunk.transcribed_at <= after


async def test_mark_chunk_transcription_failed_round_trip(db):
    session_id = await create_session(db, "continuous", started_at=0.0)
    cid = await insert_audio_chunk(db, session_id, "mic", 0.0, 30.0, "/p", 1)
    await mark_chunk_transcription_failed(db, cid, attempts=3)
    chunk = await get_chunk_by_id(db, cid)
    assert chunk.status == "transcription_failed"
    assert chunk.transcription_attempts == 3


async def test_mark_chunk_archived_clears_path(db):
    session_id = await create_session(db, "continuous", started_at=0.0)
    cid = await insert_audio_chunk(db, session_id, "mic", 0.0, 30.0, "/will/be/cleared", 1)
    await mark_chunk_archived(db, cid)
    chunk = await get_chunk_by_id(db, cid)
    assert chunk.path is None
    # Status is forced to 'transcribed' so the chunk doesn't reappear
    # in the pending queue (which filters on status = 'recorded').
    assert chunk.status == "transcribed"


async def test_mark_chunk_archived_keeps_chunk_out_of_pending_queue(db):
    """An archived chunk must NOT appear in get_pending_chunks even if it
    was archived without being transcribed first."""
    session_id = await create_session(db, "continuous", started_at=0.0)
    cid = await insert_audio_chunk(db, session_id, "mic", 0.0, 30.0, "/x", 1)
    # Sanity: it's pending before archive.
    pending = await get_pending_chunks(db)
    assert cid in {c.id for c in pending}
    await mark_chunk_archived(db, cid)
    pending_after = await get_pending_chunks(db)
    assert cid not in {c.id for c in pending_after}


async def test_mark_chunk_unavailable_round_trip(db):
    session_id = await create_session(db, "continuous", started_at=0.0)
    cid = await insert_audio_chunk(db, session_id, "mic", 0.0, 30.0, None, 0)
    await mark_chunk_unavailable(db, cid, reason="device_busy")
    chunk = await get_chunk_by_id(db, cid)
    assert chunk.status == "unavailable"
    assert chunk.unavailable_reason == "device_busy"


# ─────────────────────────────────────────────────────────────────────
# Session queries
# ─────────────────────────────────────────────────────────────────────


async def test_create_then_active_then_update_ended(db):
    sid = await create_session(db, "continuous", started_at=1000.0)
    active = await get_active_session(db)
    assert active is not None
    assert active.id == sid
    assert active.ended_at is None

    await update_session_ended(db, sid, ended_at=2000.0, end_reason="explicit_close")

    active2 = await get_active_session(db)
    assert active2 is None

    # The session row should reflect the explicit timestamp.
    sessions = await get_sessions_overlapping(db, 0.0, 5000.0)
    assert len(sessions) == 1
    assert sessions[0].id == sid
    assert sessions[0].ended_at == 2000.0
    assert sessions[0].end_reason == "explicit_close"


async def test_end_session_uses_now_unlike_update_session_ended(db):
    """The original end_session helper still uses time.time(); update_session_ended
    accepts an explicit ts for crash recovery."""
    sid = await create_session(db, "continuous", started_at=0.0)
    before = time.time()
    await end_session(db, sid, end_reason="natural")
    after = time.time()
    # Re-query
    sessions = await get_sessions_overlapping(db, 0.0, after + 100.0)
    assert len(sessions) == 1
    assert before <= sessions[0].ended_at <= after


async def test_get_sessions_overlapping_includes_active(db):
    # Active session started before window
    active_sid = await create_session(db, "continuous", started_at=500.0)
    # Closed session entirely before window
    early_sid = await create_session(db, "continuous", started_at=0.0)
    await update_session_ended(db, early_sid, ended_at=100.0, end_reason="x")
    # Closed session straddling start
    straddle_sid = await create_session(db, "continuous", started_at=900.0)
    await update_session_ended(db, straddle_sid, ended_at=1100.0, end_reason="x")
    # Closed session entirely after window
    late_sid = await create_session(db, "continuous", started_at=3000.0)
    await update_session_ended(db, late_sid, ended_at=3500.0, end_reason="x")

    sessions = await get_sessions_overlapping(db, 1000.0, 2000.0)
    ids = {s.id for s in sessions}
    assert active_sid in ids
    assert straddle_sid in ids
    assert early_sid not in ids
    assert late_sid not in ids
    for s in sessions:
        assert isinstance(s, CaptureSessionRow)


async def test_get_sessions_overlapping_excludes_active_started_after_window(db):
    """An active session whose started_at is beyond range_end should not appear."""
    sid = await create_session(db, "continuous", started_at=5000.0)
    sessions = await get_sessions_overlapping(db, 1000.0, 2000.0)
    assert sid not in {s.id for s in sessions}


# ─────────────────────────────────────────────────────────────────────
# Calendar links
# ─────────────────────────────────────────────────────────────────────


async def test_insert_and_get_session_calendar_link(db):
    sid = await create_session(db, "calendar", started_at=1000.0)
    await update_session_ended(db, sid, ended_at=2000.0, end_reason="meeting_end")

    rid = await insert_session_calendar_link(
        db,
        sid,
        calendar_event_id="evt_abc",
        overlap_start=1050.0,
        overlap_end=1950.0,
    )
    assert rid == sid

    sessions = await get_sessions_for_calendar_event(db, "evt_abc")
    assert len(sessions) == 1
    assert sessions[0].id == sid

    # Verify overlap window was stored verbatim (not the session window).
    cursor = await db.execute(
        "SELECT overlap_start, overlap_end FROM session_calendar_links "
        "WHERE session_id = ? AND calendar_event_id = ?",
        (sid, "evt_abc"),
    )
    row = await cursor.fetchone()
    assert row == (1050.0, 1950.0)


async def test_get_sessions_for_calendar_event_returns_multiple(db):
    sid_a = await create_session(db, "calendar", started_at=1000.0)
    sid_b = await create_session(db, "calendar", started_at=2000.0)
    await insert_session_calendar_link(
        db, sid_a, "evt_x", overlap_start=1000.0, overlap_end=1500.0
    )
    await insert_session_calendar_link(
        db, sid_b, "evt_x", overlap_start=2000.0, overlap_end=2500.0
    )

    sessions = await get_sessions_for_calendar_event(db, "evt_x")
    assert [s.id for s in sessions] == [sid_a, sid_b]  # ordered by started_at


async def test_get_sessions_for_calendar_event_empty(db):
    sessions = await get_sessions_for_calendar_event(db, "missing_event")
    assert sessions == []


async def test_insert_session_calendar_link_unknown_session(db):
    with pytest.raises(ValueError):
        await insert_session_calendar_link(
            db, 99999, "evt_x", overlap_start=0.0, overlap_end=1.0
        )


# ─────────────────────────────────────────────────────────────────────
# Component status
# ─────────────────────────────────────────────────────────────────────


async def test_component_status_upsert_round_trip(db):
    await upsert_component_status(db, "mic_recorder", "healthy", details="ok")
    status = await get_component_status(db, "mic_recorder")
    assert status is not None
    assert isinstance(status, ComponentStatusRow)
    assert status.component == "mic_recorder"
    assert status.status == "healthy"
    assert status.detail == "ok"


async def test_component_status_upsert_overwrites(db):
    await upsert_component_status(db, "mic_recorder", "healthy")
    await upsert_component_status(db, "mic_recorder", "down", details="device_lost")

    # Only one row should remain — no history piling up.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM component_status WHERE component = ?", ("mic_recorder",)
    )
    (n,) = await cursor.fetchone()
    assert n == 1

    status = await get_component_status(db, "mic_recorder")
    assert status.status == "down"
    assert status.detail == "device_lost"


async def test_get_component_status_missing(db):
    assert await get_component_status(db, "never_seen") is None


# ─────────────────────────────────────────────────────────────────────
# Daemon events (existing helper — touched by upsert pathway in the test
# fixture in case other tests need it). Quick smoke test for coverage.
# ─────────────────────────────────────────────────────────────────────


async def test_log_daemon_event_writes_row(db):
    await log_daemon_event(db, "info", "test", "boot", details="hello")
    cursor = await db.execute(
        "SELECT level, component, event, details FROM daemon_events"
    )
    rows = await cursor.fetchall()
    assert ("info", "test", "boot", "hello") in rows


# ─────────────────────────────────────────────────────────────────────
# Foreign-key enforcement
# ─────────────────────────────────────────────────────────────────────


async def test_audio_chunk_fk_enforcement(db):
    """Inserting a chunk with a non-existent session_id must fail.

    The conftest's tmp_db fixture sets PRAGMA foreign_keys = ON before
    yielding, so this should be enforced.
    """
    with pytest.raises(aiosqlite.IntegrityError):
        await insert_audio_chunk(
            db,
            session_id=99999,
            channel="mic",
            start_ts=0.0,
            end_ts=30.0,
            path="/x",
            samples=1,
        )
