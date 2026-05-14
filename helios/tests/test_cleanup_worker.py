"""Tests for ``helios.workers.cleanup`` — Phase 5 (Wave 5C).

Coverage:

* Transcribed WAVs older than ``raw_audio_days`` get moved to trash and
  the DB row is archived (``path = NULL``, status flipped).
* Untranscribed WAVs ARE PRESERVED — never trashed, no matter how old.
* Trash files older than ``trash_hold_hours`` are permanently deleted.
* Trash files within the hold window survive a cleanup pass.
* :func:`trash_session_audio` moves every chunk's WAV for one session
  out of the live tree and returns the right count.
* Worker lifecycle: ``start()`` / ``stop()`` are idempotent.
* The daily tick fires after the next ``cleanup_hour_local`` and not before.
"""

from __future__ import annotations

import asyncio
import time
import wave
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio

from helios.clock import VirtualClock
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.migrations import run_migrations
from helios.workers.cleanup import (
    CleanupReport,
    CleanupWorker,
    trash_dir_for,
    trash_session_audio,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_db, migrations_dir):
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


@pytest.fixture
def cfg(tmp_path) -> HeliosConfig:
    """Config with storage rooted in tmp_path so trash dirs are isolated."""
    c = HeliosConfig()
    c.storage.root = str(tmp_path / "capture")
    # Reasonable defaults; individual tests override.
    c.retention.raw_audio_days = 7
    c.retention.trash_hold_hours = 24
    c.retention.cleanup_hour_local = 3
    return c


@pytest.fixture
def storage_root(cfg: HeliosConfig) -> Path:
    root = Path(cfg.storage.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_wav(path: Path, *, samples: int = 16000) -> None:
    """Write a tiny silent 16 kHz mono 16-bit PCM WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = np.zeros(samples, dtype=np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(frames.tobytes())


async def _make_chunk(
    db,
    *,
    storage_root: Path,
    session_id: int | None = None,
    channel: str = "mic",
    start_ts: float,
    status: str = "transcribed",
    path_name: str | None = None,
    write_file: bool = True,
) -> int:
    """Helper: create a chunk row + optional WAV file. Returns chunk id."""
    if session_id is None:
        session_id = await queries.create_session(
            db, "manual_screen", started_at=start_ts
        )
    file_name = path_name or f"chunk_{int(start_ts)}_{channel}.wav"
    wav_path = storage_root / "audio" / file_name
    if write_file:
        _write_wav(wav_path)
    cid = await queries.insert_audio_chunk(
        db,
        session_id=session_id,
        channel=channel,
        start_ts=start_ts,
        end_ts=start_ts + 30.0,
        path=str(wav_path),
        samples=16000 * 30,
    )
    # Optionally flip status (insert_audio_chunk defaults to 'recorded').
    if status == "transcribed":
        await queries.mark_chunk_transcribed(db, cid)
    elif status == "no_audio":
        # No clean helper; do a targeted UPDATE.
        await db.execute(
            "UPDATE audio_chunks SET status='no_audio' WHERE id=?", (cid,)
        )
        await db.commit()
    elif status == "transcription_failed":
        await queries.mark_chunk_transcription_failed(db, cid, attempts=3)
    elif status == "unavailable":
        await queries.mark_chunk_unavailable(db, cid, reason="capture_stall")
    # 'recorded' is the default — nothing to do.
    return cid


# ---------------------------------------------------------------------------
# trash_session_audio — used by DELETE /v1/sessions/{id}
# ---------------------------------------------------------------------------


async def test_trash_session_audio_moves_files_and_counts(
    db, cfg, storage_root, virtual_clock
):
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    cid1 = await _make_chunk(
        db, storage_root=storage_root, session_id=sid,
        channel="mic", start_ts=1_000.0,
    )
    cid2 = await _make_chunk(
        db, storage_root=storage_root, session_id=sid,
        channel="system", start_ts=1_030.0,
    )

    moved = await trash_session_audio(
        db, session_id=sid, storage_root=storage_root
    )
    assert moved == 2

    trash_dir = trash_dir_for(storage_root)
    trash_files = sorted(p.name for p in trash_dir.iterdir())
    # Each file is prefixed with chunk_id so the names are unique.
    assert any(str(cid1) in name for name in trash_files)
    assert any(str(cid2) in name for name in trash_files)

    # Source files are gone.
    for chunk in await queries.get_session_audio_chunks(db, sid):
        assert not Path(chunk.path).exists()


async def test_trash_session_audio_skips_chunks_with_no_path(
    db, cfg, storage_root
):
    """Already-archived chunks (path NULL) are not counted."""
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    # One real chunk
    cid_real = await _make_chunk(
        db, storage_root=storage_root, session_id=sid,
        channel="mic", start_ts=1_000.0,
    )
    # One "no_audio" chunk — no file on disk, NULL path
    await queries.insert_no_audio_chunk(
        db, session_id=sid, channel="mic",
        start_ts=2_000.0, end_ts=2_030.0, samples=16000 * 30,
    )

    moved = await trash_session_audio(
        db, session_id=sid, storage_root=storage_root
    )
    assert moved == 1


async def test_trash_session_audio_survives_missing_file(
    db, cfg, storage_root
):
    """If the WAV vanished out from under us the helper logs and continues."""
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    cid = await _make_chunk(
        db, storage_root=storage_root, session_id=sid,
        channel="mic", start_ts=1_000.0,
    )
    # Delete the file directly to simulate disappearance
    chunks = await queries.get_session_audio_chunks(db, sid)
    Path(chunks[0].path).unlink()

    moved = await trash_session_audio(
        db, session_id=sid, storage_root=storage_root
    )
    assert moved == 0  # File was gone


# ---------------------------------------------------------------------------
# CleanupWorker.run_cleanup — archive transcribed-and-old
# ---------------------------------------------------------------------------


async def test_run_cleanup_archives_old_transcribed_wavs(
    db, cfg, storage_root, virtual_clock
):
    """Old + transcribed → trashed; row's path cleared, status terminal."""
    cfg.retention.raw_audio_days = 7
    now = 10_000_000.0
    virtual_clock._now = now  # set virtual time

    eight_days_ago = now - (8 * 86400)
    cid_old = await _make_chunk(
        db, storage_root=storage_root,
        channel="mic", start_ts=eight_days_ago,
        status="transcribed",
    )
    # And a fresh chunk we must NOT touch.
    cid_fresh = await _make_chunk(
        db, storage_root=storage_root,
        channel="mic", start_ts=now - 3600,
        status="transcribed",
    )

    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    report = await worker.run_cleanup()
    assert report.archived == 1
    assert report.errors == 0

    # Old chunk: path cleared, status terminal.
    old_row = await queries.get_chunk_by_id(db, cid_old)
    assert old_row is not None
    assert old_row.path is None
    assert old_row.status == "transcribed"

    # Fresh chunk: untouched.
    fresh_row = await queries.get_chunk_by_id(db, cid_fresh)
    assert fresh_row is not None
    assert fresh_row.path is not None
    assert Path(fresh_row.path).exists()

    # Trash contains the moved file.
    trash_dir = trash_dir_for(storage_root)
    trashed = list(trash_dir.iterdir())
    assert len(trashed) == 1
    assert str(cid_old) in trashed[0].name


async def test_run_cleanup_preserves_untranscribed_old_wavs(
    db, cfg, storage_root, virtual_clock
):
    """Old WAVs with non-transcribed status must NOT go to trash.

    This is the safety property §17 calls out — the user never loses
    content that hasn't been processed yet.
    """
    cfg.retention.raw_audio_days = 7
    now = 10_000_000.0
    virtual_clock._now = now
    eight_days_ago = now - (8 * 86400)

    cid_recorded = await _make_chunk(
        db, storage_root=storage_root,
        channel="mic", start_ts=eight_days_ago,
        status="recorded",
    )
    cid_failed = await _make_chunk(
        db, storage_root=storage_root,
        channel="mic", start_ts=eight_days_ago + 100,
        status="transcription_failed",
    )
    cid_no_audio_row_id = await queries.create_session(
        db, "manual_screen", started_at=eight_days_ago
    )
    # no_audio is stored without a path, but we still want to verify
    # the worker doesn't blow up on it.
    await queries.insert_no_audio_chunk(
        db, session_id=cid_no_audio_row_id, channel="mic",
        start_ts=eight_days_ago + 200, end_ts=eight_days_ago + 230,
        samples=16000 * 30,
    )

    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    report = await worker.run_cleanup()
    assert report.archived == 0
    # Both untranscribed-with-path chunks are counted as skipped.
    assert report.skipped_untranscribed == 2

    # Files still on disk.
    rec = await queries.get_chunk_by_id(db, cid_recorded)
    fail = await queries.get_chunk_by_id(db, cid_failed)
    assert rec is not None and rec.path is not None
    assert fail is not None and fail.path is not None
    assert Path(rec.path).exists()
    assert Path(fail.path).exists()

    # Trash dir empty.
    trash = trash_dir_for(storage_root)
    assert list(trash.iterdir()) == []


# ---------------------------------------------------------------------------
# CleanupWorker — trash sweep
# ---------------------------------------------------------------------------


async def test_run_cleanup_purges_old_trash_files(
    db, cfg, storage_root, virtual_clock
):
    """Files in trash with mtime older than trash_hold_hours → deleted."""
    cfg.retention.trash_hold_hours = 24
    now = 10_000_000.0
    virtual_clock._now = now

    trash_dir = trash_dir_for(storage_root)
    # Make two files in trash: one stale, one fresh.
    stale = trash_dir / "1_old.wav"
    fresh = trash_dir / "2_new.wav"
    _write_wav(stale)
    _write_wav(fresh)

    # Backdate stale mtime to 48h before "now" (now is virtual_clock.time()
    # which is `now`, but os.stat uses real wall-clock). Use os.utime to
    # set both atime/mtime to a value < virtual_clock.time() - 24*3600.
    import os

    stale_real_mtime = now - (48 * 3600)
    fresh_real_mtime = now - (1 * 3600)
    os.utime(stale, (stale_real_mtime, stale_real_mtime))
    os.utime(fresh, (fresh_real_mtime, fresh_real_mtime))

    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    report = await worker.run_cleanup()
    assert report.purged == 1

    assert not stale.exists()
    assert fresh.exists()


async def test_run_cleanup_keeps_fresh_trash_within_hold(
    db, cfg, storage_root, virtual_clock
):
    """Trash file inside the hold window must survive."""
    cfg.retention.trash_hold_hours = 24
    now = 10_000_000.0
    virtual_clock._now = now

    trash_dir = trash_dir_for(storage_root)
    fresh = trash_dir / "1_recent.wav"
    _write_wav(fresh)
    # 6 hours old — well inside the 24 hour hold.
    import os

    os.utime(fresh, (now - (6 * 3600), now - (6 * 3600)))

    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    report = await worker.run_cleanup()
    assert report.purged == 0
    assert fresh.exists()


# ---------------------------------------------------------------------------
# CleanupWorker — scheduling
# ---------------------------------------------------------------------------


def test_seconds_until_next_run_picks_today_if_in_future(
    db, cfg, storage_root, virtual_clock
):
    """At 1:00 AM local, the 3:00 AM cleanup fires today (in ~2 hours)."""
    cfg.retention.cleanup_hour_local = 3
    # Pick a virtual time at 1 AM local. VirtualClock.now_local just
    # interprets the float as a local-time epoch via fromtimestamp,
    # so we construct the float from a known datetime.
    import datetime as _dt

    target = _dt.datetime(2026, 5, 14, 1, 0, 0)  # 1 AM
    virtual_clock._now = target.timestamp()

    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    delay = worker._seconds_until_next_run()
    # ~2 hours, within a minute either way.
    assert 7100 < delay < 7300


def test_seconds_until_next_run_picks_tomorrow_when_past_today(
    db, cfg, storage_root, virtual_clock
):
    """At 5:00 AM, the next 3 AM run is ~22 hours away."""
    cfg.retention.cleanup_hour_local = 3
    import datetime as _dt

    target = _dt.datetime(2026, 5, 14, 5, 0, 0)  # 5 AM
    virtual_clock._now = target.timestamp()

    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    delay = worker._seconds_until_next_run()
    # ~22 hours.
    assert 22 * 3600 - 60 < delay < 22 * 3600 + 60


# ---------------------------------------------------------------------------
# CleanupWorker — lifecycle
# ---------------------------------------------------------------------------


async def test_worker_start_is_idempotent(db, cfg, virtual_clock):
    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    await worker.start()
    first = worker._loop_task
    await worker.start()
    assert worker._loop_task is first
    await worker.stop()


async def test_worker_stop_is_idempotent(db, cfg, virtual_clock):
    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    await worker.start()
    await worker.stop()
    # Second stop must not raise.
    await worker.stop()
    assert worker._loop_task is None


async def test_daily_tick_runs_cleanup_after_delay(
    db, cfg, storage_root, virtual_clock
):
    """Advance the virtual clock past the next fire time → cleanup runs."""
    cfg.retention.cleanup_hour_local = 3
    cfg.retention.raw_audio_days = 7
    cfg.retention.trash_hold_hours = 24

    import datetime as _dt

    # Start at 1 AM local.
    one_am = _dt.datetime(2026, 5, 14, 1, 0, 0)
    virtual_clock._now = one_am.timestamp()

    # Seed an old transcribed chunk so the cleanup has something to do.
    eight_days_ago = virtual_clock.time() - (8 * 86400)
    cid = await _make_chunk(
        db, storage_root=storage_root,
        channel="mic", start_ts=eight_days_ago,
        status="transcribed",
    )

    worker = CleanupWorker(db=db, config=cfg, clock=virtual_clock)
    await worker.start()
    # Yield so the worker's loop runs and registers its first sleeper
    # *before* we advance the virtual clock; without this the
    # advance() call sees an empty sleeper list and silently no-ops.
    for _ in range(3):
        await asyncio.sleep(0)
    try:
        # Advance past 3 AM — the daily tick should fire.
        await virtual_clock.advance(3 * 3600)
        # Wait for the cleanup task to drain (aiosqlite uses a real
        # executor thread, so we need wall-clock time, not just
        # asyncio.sleep(0) yields).
        async def _archived() -> bool:
            row = await queries.get_chunk_by_id(db, cid)
            return row is not None and row.path is None

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if await _archived():
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover
            pytest.fail("cleanup never archived the old chunk")
        row = await queries.get_chunk_by_id(db, cid)
        assert row is not None
        assert row.path is None  # archived
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# CleanupWorker — report fields
# ---------------------------------------------------------------------------


def test_cleanup_report_defaults():
    r = CleanupReport()
    assert r.archived == 0
    assert r.skipped_untranscribed == 0
    assert r.purged == 0
    assert r.errors == 0
