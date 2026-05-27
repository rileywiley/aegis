"""Tests for ``helios.capture.chunker.Chunker``.

Covers:
- Full-chunk flush at exactly chunk_seconds * sample_rate samples
- Carryover when more than one chunk's worth arrives in a single block
- Partial flush at session stop
- Silent buffers → no_audio rows with NULL path
- Independent per-channel buffering
- WAV file integrity (frame count, sample rate, channels)
- DB row consistency with the on-disk WAV
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from helios.capture.chunker import Chunker
from helios.config import AudioConfig
from helios.db import queries
from helios.db.migrations import run_migrations


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _sine(n_samples: int, freq_hz: float = 440.0, sr: int = 16000, amp: int = 8000) -> np.ndarray:
    """Return ``n_samples`` of a mono int16 sine wave."""
    t = np.arange(n_samples, dtype=np.float32) / sr
    wave_arr = (amp * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)
    return wave_arr


def _silence(n_samples: int) -> np.ndarray:
    """Return ``n_samples`` of int16 zeros (perfect silence, RMS = 0 → -inf dB)."""
    return np.zeros(n_samples, dtype=np.int16)


@pytest.fixture
def audio_cfg() -> AudioConfig:
    return AudioConfig()


@pytest.fixture
async def migrated_db(tmp_db, migrations_dir):
    """``tmp_db`` with the initial migration applied so chunks/sessions tables exist."""
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


@pytest.fixture
async def session_id(migrated_db):
    """A real ``capture_sessions`` row; chunker FK requires it."""
    return await queries.create_session(migrated_db, kind="manual_screen", started_at=1712345000.0)


@pytest.fixture
def chunker(session_id, tmp_path, migrated_db, audio_cfg, virtual_clock) -> Chunker:
    return Chunker(
        session_id=session_id,
        storage_root=tmp_path / "storage",
        db=migrated_db,
        config=audio_cfg,
        clock=virtual_clock,
    )


# ---------------------------------------------------------------------------
# Full-chunk flush
# ---------------------------------------------------------------------------


async def test_30s_of_samples_produces_one_full_chunk(
    chunker, migrated_db, audio_cfg, session_id
):
    """Exactly 30s of audio → 1 row, status='recorded', WAV file on disk."""
    n = audio_cfg.sample_rate * audio_cfg.chunk_seconds  # 480_000
    data = _sine(n)
    await chunker.add_samples("mic", data, ts=1712345000.0)

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "mic"
    assert row.partial is False
    assert row.samples == n
    assert row.status == "recorded"
    assert row.path is not None
    assert Path(row.path).exists()

    # Validate the WAV on disk.
    with wave.open(row.path, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == audio_cfg.sample_rate
        assert wf.getnframes() == n


async def test_dbrow_matches_wav(chunker, migrated_db, audio_cfg, session_id):
    """start_ts / end_ts / samples / path are mutually consistent."""
    n = audio_cfg.sample_rate * audio_cfg.chunk_seconds
    start = 1712345000.0
    await chunker.add_samples("mic", _sine(n), ts=start)

    [row] = await queries.get_session_audio_chunks(migrated_db, session_id)
    expected_end = start + (n / audio_cfg.sample_rate)
    assert row.start_ts == pytest.approx(start)
    assert row.end_ts == pytest.approx(expected_end)
    with wave.open(row.path, "rb") as wf:
        assert wf.getnframes() == row.samples


# ---------------------------------------------------------------------------
# Carryover and partial flush
# ---------------------------------------------------------------------------


async def test_35s_emits_one_full_then_buffers_remainder(
    chunker, migrated_db, audio_cfg, session_id
):
    """35s of audio: one 30s chunk written, 5s remains buffered (no row yet)."""
    sr = audio_cfg.sample_rate
    n = sr * 35
    await chunker.add_samples("mic", _sine(n), ts=1712345000.0)

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert len(rows) == 1, "exactly one chunk should have been flushed"
    assert rows[0].samples == sr * audio_cfg.chunk_seconds
    assert rows[0].partial is False


async def test_flush_partial_emits_remaining_buffer_as_partial(
    chunker, migrated_db, audio_cfg, session_id
):
    """Leftover samples after stop → partial=1 row."""
    sr = audio_cfg.sample_rate
    # Push 35s, then stop. 30s already flushed; 5s should partial-flush.
    await chunker.add_samples("mic", _sine(sr * 35), ts=1712345000.0)
    await chunker.flush_partial(reason="test_stop")

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert len(rows) == 2

    # Order by start_ts is guaranteed by get_session_audio_chunks
    full, partial = rows
    assert full.partial is False
    assert full.samples == sr * audio_cfg.chunk_seconds
    assert partial.partial is True
    assert partial.samples == sr * 5
    assert partial.status == "recorded"
    assert partial.start_ts == pytest.approx(full.end_ts)


async def test_flush_partial_skips_empty_buffers(chunker, migrated_db, session_id):
    """A flush_partial with nothing buffered must not insert any rows."""
    await chunker.flush_partial()
    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert rows == []


# ---------------------------------------------------------------------------
# Silence handling
# ---------------------------------------------------------------------------


async def test_silent_chunk_writes_no_audio_row_with_null_path(
    chunker, migrated_db, audio_cfg, session_id, tmp_path
):
    """All-zeros 30s → status='no_audio', path IS NULL, no WAV on disk."""
    n = audio_cfg.sample_rate * audio_cfg.chunk_seconds
    await chunker.add_samples("mic", _silence(n), ts=1712345000.0)

    [row] = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert row.status == "no_audio"
    assert row.path is None
    assert row.samples == n
    assert row.partial is False

    # No WAV file should have been written under the storage root.
    storage_root = tmp_path / "storage"
    if storage_root.exists():
        wavs = list(storage_root.rglob("*.wav"))
        assert wavs == []


async def test_silent_partial_flush_records_no_audio(
    chunker, migrated_db, audio_cfg, session_id
):
    """Silent leftover after stop should still produce a no_audio partial row."""
    n = audio_cfg.sample_rate * 5  # 5s of silence
    await chunker.add_samples("mic", _silence(n), ts=1712345000.0)
    await chunker.flush_partial(reason="stopping")

    [row] = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert row.status == "no_audio"
    assert row.partial is True
    assert row.path is None
    assert row.samples == n


# ---------------------------------------------------------------------------
# Independent per-channel buffering
# ---------------------------------------------------------------------------


async def test_channels_buffer_independently(
    chunker, migrated_db, audio_cfg, session_id
):
    """Mic and system buffers flush independently and produce separate rows."""
    sr = audio_cfg.sample_rate
    chunk_n = sr * audio_cfg.chunk_seconds

    # Interleave smaller blocks across channels — each channel's accumulator
    # advances independently.
    block = sr * 5  # 5s blocks
    ts = 1712345000.0
    # 30s of mic in 6 blocks, intermixed with 30s of system in 6 blocks
    for i in range(6):
        await chunker.add_samples("mic", _sine(block, freq_hz=440), ts=ts + i * 5)
        await chunker.add_samples("system", _sine(block, freq_hz=880), ts=ts + i * 5)

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    by_channel = {}
    for r in rows:
        by_channel.setdefault(r.channel, []).append(r)
    assert set(by_channel.keys()) == {"mic", "system"}
    assert len(by_channel["mic"]) == 1
    assert len(by_channel["system"]) == 1
    assert by_channel["mic"][0].samples == chunk_n
    assert by_channel["system"][0].samples == chunk_n

    # Filenames should distinguish channels by their parent dir.
    mic_path = Path(by_channel["mic"][0].path)
    sys_path = Path(by_channel["system"][0].path)
    assert mic_path.parent.name == "mic"
    assert sys_path.parent.name == "system"
    assert mic_path != sys_path


async def test_independent_channel_partial_flush(
    chunker, migrated_db, audio_cfg, session_id
):
    """Only channels with buffered audio emit partial rows on stop."""
    sr = audio_cfg.sample_rate
    # Mic gets 5s; system gets nothing.
    await chunker.add_samples("mic", _sine(sr * 5), ts=1712345000.0)
    await chunker.flush_partial(reason="stop")

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert len(rows) == 1
    assert rows[0].channel == "mic"
    assert rows[0].partial is True


# ---------------------------------------------------------------------------
# WAV content / file naming
# ---------------------------------------------------------------------------


async def test_wav_frames_match_concatenated_samples(
    chunker, migrated_db, audio_cfg, session_id
):
    """Reading back the WAV gives back the exact bytes we wrote."""
    sr = audio_cfg.sample_rate
    n = sr * audio_cfg.chunk_seconds
    data = _sine(n, freq_hz=220, amp=10000)
    await chunker.add_samples("mic", data, ts=1712345000.0)

    [row] = await queries.get_session_audio_chunks(migrated_db, session_id)
    with wave.open(row.path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    decoded = np.frombuffer(raw, dtype=np.int16)
    assert decoded.shape == (n,)
    assert np.array_equal(decoded, data)


async def test_chunk_filenames_are_monotonic_per_channel(
    chunker, migrated_db, audio_cfg, session_id
):
    """Successive flushes for the same channel use increasing chunk indexes."""
    sr = audio_cfg.sample_rate
    chunk_n = sr * audio_cfg.chunk_seconds
    # Push 90s in one go → 3 full chunks for mic.
    await chunker.add_samples("mic", _sine(chunk_n * 3), ts=1712345000.0)

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert len(rows) == 3
    names = [Path(r.path).name for r in rows]
    # All under the same session_id prefix; indexes 00000, 00001, 00002.
    assert names == [
        f"{session_id}_00000.wav",
        f"{session_id}_00001.wav",
        f"{session_id}_00002.wav",
    ]


async def test_path_includes_local_date_from_clock(
    chunker, migrated_db, audio_cfg, session_id, virtual_clock
):
    """The day-bucket directory comes from clock.now_local(), not wall clock."""
    sr = audio_cfg.sample_rate
    n = sr * audio_cfg.chunk_seconds
    await chunker.add_samples("mic", _sine(n), ts=virtual_clock.time())

    [row] = await queries.get_session_audio_chunks(migrated_db, session_id)
    expected_date = virtual_clock.now_local().strftime("%Y-%m-%d")
    assert expected_date in row.path


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_empty_block_is_noop(chunker, migrated_db, session_id):
    """add_samples with zero-length array does nothing (no row, no error)."""
    await chunker.add_samples("mic", np.zeros(0, dtype=np.int16), ts=1712345000.0)
    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert rows == []


async def test_unknown_channel_raises(chunker):
    """Bad channel names are rejected up front."""
    with pytest.raises(ValueError):
        await chunker.add_samples("speaker", _sine(100), ts=1712345000.0)  # type: ignore[arg-type]


async def test_dtype_coerced_to_int16(
    chunker, migrated_db, audio_cfg, session_id
):
    """Float input gets coerced to int16 before being buffered/written."""
    sr = audio_cfg.sample_rate
    n = sr * audio_cfg.chunk_seconds
    # Construct a clearly-non-silent float32 signal.
    f = (8000 * np.sin(2 * np.pi * 440 * np.arange(n) / sr)).astype(np.float32)
    await chunker.add_samples("mic", f, ts=1712345000.0)

    [row] = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert row.status == "recorded"
    with wave.open(row.path, "rb") as wf:
        assert wf.getsampwidth() == 2  # int16
        assert wf.getnframes() == n


async def test_two_full_chunks_in_single_add(
    chunker, migrated_db, audio_cfg, session_id
):
    """A single add_samples carrying >= 2 chunks worth flushes both."""
    sr = audio_cfg.sample_rate
    chunk_n = sr * audio_cfg.chunk_seconds
    # 65s of audio in one block → two 30s chunks + 5s buffered.
    await chunker.add_samples("mic", _sine(chunk_n * 2 + sr * 5), ts=1712345000.0)

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert len(rows) == 2
    assert all(r.samples == chunk_n for r in rows)
    # Second chunk's start_ts must equal first chunk's end_ts.
    assert rows[1].start_ts == pytest.approx(rows[0].end_ts)
