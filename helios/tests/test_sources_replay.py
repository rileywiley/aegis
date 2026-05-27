"""Tests for replay sources (Track 1B.2).

These tests use VirtualClock to drive replay sources deterministically — no
real wall-clock waits. The pattern: start the source, await asyncio.sleep(0)
to let the emit loop register its first sleep, then advance virtual time and
drain the outbound queue.
"""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from helios.sources.interface import (
    AudioSample,
    CalendarEvent,
)
from helios.sources.replay import (
    ReplayCalendarSource,
    ReplayMicSource,
    ReplaySystemAudioSource,
)
from tests.conftest import synth_wav


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain_blocks(
    queue: asyncio.Queue[AudioSample],
    max_blocks: int,
    timeout: float = 0.5,
) -> list[AudioSample]:
    """Pull up to `max_blocks` from the queue, stopping early on timeout."""
    out: list[AudioSample] = []
    for _ in range(max_blocks):
        try:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        out.append(item)
    return out


async def _drain_via_samples(
    src,
    max_blocks: int,
    timeout: float = 0.5,
) -> list[AudioSample]:
    """Drain up to `max_blocks` from a source's samples() iterator."""
    out: list[AudioSample] = []
    iterator = src.samples()
    for _ in range(max_blocks):
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        out.append(item)
    return out


async def _settle() -> None:
    """Yield several times so newly-registered sleepers get scheduled."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# ReplayMicSource — happy path
# ---------------------------------------------------------------------------


async def test_replay_mic_emits_expected_blocks(tmp_path, virtual_clock):
    """A 5-second WAV at blocksize=1600 (100ms) emits 50 mic blocks with rising ts."""
    wav = synth_wav(tmp_path / "mic_5s.wav", duration_s=5.0)
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()

    src = ReplayMicSource(
        wav_path=wav,
        queue=queue,
        clock=virtual_clock,
        start_ts=virtual_clock.time(),
        speed_multiplier=10.0,
        blocksize=1600,
    )

    # Spin up the consumer BEFORE starting the source so the iterator
    # queue captures every emitted block.
    blocks: list[AudioSample] = []

    async def consumer() -> None:
        async for s in src.samples():
            blocks.append(s)

    consumer_task = asyncio.create_task(consumer())

    await src.start()
    assert src.is_running

    # 5 seconds @ 10x with 100ms-per-block sleeps means each emit waits 10ms
    # of virtual time. Advance well past the WAV's worth of work.
    await _settle()
    # Each block needs sleep_per_block = 0.1 / 10 = 0.01s of virtual time.
    # Advance comfortably past 50 * 0.01 = 0.5s.
    await virtual_clock.advance(2.0)
    await _settle()

    await src.stop()
    await asyncio.wait_for(consumer_task, timeout=1.0)

    # 5s * 16000Hz / 1600 samples/block = 50 blocks exactly.
    assert len(blocks) == 50
    assert all(b.channel == "mic" for b in blocks)
    assert all(b.samples.dtype == np.int16 for b in blocks)
    assert all(b.samples.shape == (1600,) for b in blocks)

    # Timestamps are spaced by 0.1s (real-time scale, NOT divided by speed).
    start = blocks[0].ts
    for i, b in enumerate(blocks):
        assert b.ts == pytest.approx(start + i * 0.1, abs=1e-9)


async def test_replay_mic_partial_final_block(tmp_path, virtual_clock):
    """A WAV whose length is not a multiple of blocksize emits a short final block."""
    # 5.05s = 80800 samples → 50 full + 1 partial of 800 samples.
    wav = synth_wav(tmp_path / "mic_oddlen.wav", duration_s=5.05)
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplayMicSource(wav, queue, virtual_clock, speed_multiplier=10.0)

    blocks: list[AudioSample] = []

    async def consumer() -> None:
        async for s in src.samples():
            blocks.append(s)

    consumer_task = asyncio.create_task(consumer())
    await src.start()
    await _settle()
    await virtual_clock.advance(2.0)
    await _settle()
    await src.stop()
    await asyncio.wait_for(consumer_task, timeout=1.0)

    assert len(blocks) == 51
    assert blocks[-1].samples.shape == (800,)
    assert blocks[0].samples.shape == (1600,)


# ---------------------------------------------------------------------------
# ReplayMicSource — validation
# ---------------------------------------------------------------------------


async def test_replay_mic_rejects_wrong_sample_rate(tmp_path, virtual_clock):
    """An 8kHz WAV must be rejected at start() time with a ValueError."""
    wav = synth_wav(tmp_path / "mic_8k.wav", duration_s=1.0, sample_rate=8000)
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplayMicSource(wav, queue, virtual_clock)

    with pytest.raises(ValueError, match="sample_rate=8000"):
        await src.start()
    assert not src.is_running


async def test_replay_mic_rejects_stereo(tmp_path, virtual_clock):
    """Stereo (2-channel) WAV must be rejected."""
    wav = synth_wav(tmp_path / "mic_stereo.wav", duration_s=0.1, n_channels=2)
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplayMicSource(wav, queue, virtual_clock)

    with pytest.raises(ValueError, match="channels=2"):
        await src.start()


async def test_replay_mic_missing_file(tmp_path, virtual_clock):
    """A missing WAV path raises FileNotFoundError on start()."""
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplayMicSource(
        tmp_path / "does_not_exist.wav", queue, virtual_clock
    )
    with pytest.raises(FileNotFoundError):
        await src.start()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_start_stop_lifecycle(tmp_path, virtual_clock):
    """is_running flips correctly across start/stop; samples() iterator terminates."""
    wav = synth_wav(tmp_path / "lc.wav", duration_s=1.0)
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplayMicSource(wav, queue, virtual_clock, speed_multiplier=10.0)

    assert not src.is_running

    iter_obj = src.samples()
    collected: list[AudioSample] = []

    async def consumer() -> None:
        async for s in iter_obj:
            collected.append(s)

    consumer_task = asyncio.create_task(consumer())

    await src.start()
    assert src.is_running

    await _settle()
    # Pull a couple of blocks worth of virtual time, then stop early.
    await virtual_clock.advance(0.05)  # 5 blocks @ 10x
    await _settle()

    await src.stop()
    assert not src.is_running

    # Consumer must terminate cleanly (sentinel) even though we stopped early.
    await asyncio.wait_for(consumer_task, timeout=1.0)

    # We pulled some samples before stopping; exact count depends on event-loop
    # ordering, just confirm > 0 and channel == "mic".
    assert len(collected) > 0
    assert all(s.channel == "mic" for s in collected)


async def test_double_start_is_noop(tmp_path, virtual_clock):
    """Calling start() while already running does not spawn a second task."""
    wav = synth_wav(tmp_path / "noop.wav", duration_s=1.0)
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplayMicSource(wav, queue, virtual_clock)

    await src.start()
    first_task = src._task
    await src.start()  # should be a no-op
    assert src._task is first_task

    await src.stop()


async def test_double_stop_is_safe(tmp_path, virtual_clock):
    """Calling stop() twice does not raise."""
    wav = synth_wav(tmp_path / "stop2.wav", duration_s=0.5)
    queue: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplayMicSource(wav, queue, virtual_clock)

    await src.start()
    await src.stop()
    await src.stop()  # second call must not raise


# ---------------------------------------------------------------------------
# Mic + System synchronization
# ---------------------------------------------------------------------------


async def test_mic_and_system_sources_synchronized(tmp_path, virtual_clock):
    """Mic + system replay sources started at the same start_ts emit aligned timestamps."""
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=2.0, freq_hz=440.0)
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=2.0, freq_hz=880.0)

    mic_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    sys_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    shared_start = virtual_clock.time()

    mic = ReplayMicSource(
        mic_wav, mic_q, virtual_clock, start_ts=shared_start, speed_multiplier=10.0
    )
    sysrc = ReplaySystemAudioSource(
        sys_wav, sys_q, virtual_clock, start_ts=shared_start, speed_multiplier=10.0
    )

    mic_blocks: list[AudioSample] = []
    sys_blocks: list[AudioSample] = []

    async def mic_consumer() -> None:
        async for s in mic.samples():
            mic_blocks.append(s)

    async def sys_consumer() -> None:
        async for s in sysrc.samples():
            sys_blocks.append(s)

    mic_task = asyncio.create_task(mic_consumer())
    sys_task = asyncio.create_task(sys_consumer())

    await mic.start()
    await sysrc.start()
    await _settle()
    await virtual_clock.advance(1.0)
    await _settle()

    await mic.stop()
    await sysrc.stop()
    await asyncio.wait_for(mic_task, timeout=1.0)
    await asyncio.wait_for(sys_task, timeout=1.0)

    # Both should have emitted exactly 20 blocks (2s / 0.1s per block).
    assert len(mic_blocks) == 20
    assert len(sys_blocks) == 20
    assert mic_blocks[0].channel == "mic"
    assert sys_blocks[0].channel == "system"

    # Per-block timestamps must match across channels.
    for m, s in zip(mic_blocks, sys_blocks):
        assert m.ts == pytest.approx(s.ts, abs=1e-9)


async def test_system_source_video_frames_not_implemented(tmp_path, virtual_clock):
    """ReplaySystemAudioSource.video_frames() raises in Phase 1."""
    sys_wav = synth_wav(tmp_path / "v.wav", duration_s=0.1)
    q: asyncio.Queue[AudioSample] = asyncio.Queue()
    src = ReplaySystemAudioSource(sys_wav, q, virtual_clock)
    with pytest.raises(NotImplementedError, match="video frames"):
        src.video_frames()


# ---------------------------------------------------------------------------
# ReplayCalendarSource
# ---------------------------------------------------------------------------


def _write_cal(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(events))
    return path


async def test_replay_calendar_filters_by_horizon(tmp_path, virtual_clock):
    """get_upcoming returns events within [now, now+horizon], sorted by start_ts."""
    now = virtual_clock.time()
    events = [
        # past event — excluded
        {"id": "past", "title": "Past", "start_ts": now - 100, "end_ts": now - 50},
        # within horizon, returned in order by start_ts
        {
            "id": "soon",
            "title": "Soon",
            "start_ts": now + 60,
            "end_ts": now + 120,
            "attendees": ["a@x.io"],
        },
        {"id": "later", "title": "Later", "start_ts": now + 30, "end_ts": now + 40},
        # beyond horizon — excluded
        {"id": "far", "title": "Far", "start_ts": now + 9999, "end_ts": now + 10000},
    ]
    cal_path = _write_cal(tmp_path / "cal.json", events)
    src = ReplayCalendarSource(cal_path, virtual_clock)

    upcoming = await src.get_upcoming(horizon_seconds=300)

    assert [e.id for e in upcoming] == ["later", "soon"]
    assert all(isinstance(e, CalendarEvent) for e in upcoming)
    assert upcoming[1].attendees == ["a@x.io"]


async def test_replay_calendar_includes_in_progress_events(tmp_path, virtual_clock):
    """Events whose window overlaps [now, now+horizon) are returned even if
    they started in the past — so a scheduler waking mid-meeting can attach."""
    now = virtual_clock.time()
    events = [
        # Started 30s ago, ends in 25 minutes — currently in progress.
        {
            "id": "in_progress",
            "title": "Standup",
            "start_ts": now - 30,
            "end_ts": now + 1500,
        },
        # Already over — must NOT be included.
        {
            "id": "ended",
            "title": "Yesterday",
            "start_ts": now - 1000,
            "end_ts": now - 100,
        },
    ]
    cal_path = _write_cal(tmp_path / "ip.json", events)
    src = ReplayCalendarSource(cal_path, virtual_clock)
    upcoming = await src.get_upcoming(horizon_seconds=300)
    ids = [e.id for e in upcoming]
    assert "in_progress" in ids
    assert "ended" not in ids


async def test_replay_calendar_advances_with_clock(tmp_path, virtual_clock):
    """Advancing the clock excludes events that fell into the past."""
    now = virtual_clock.time()
    events = [
        {"id": "a", "title": "A", "start_ts": now + 10, "end_ts": now + 20},
        {"id": "b", "title": "B", "start_ts": now + 100, "end_ts": now + 200},
    ]
    cal_path = _write_cal(tmp_path / "cal.json", events)
    src = ReplayCalendarSource(cal_path, virtual_clock)

    assert {e.id for e in await src.get_upcoming(300)} == {"a", "b"}

    # Move past event A.
    await virtual_clock.advance(30)
    assert {e.id for e in await src.get_upcoming(300)} == {"b"}


async def test_replay_calendar_missing_file(tmp_path, virtual_clock):
    """Constructing with a missing path raises immediately."""
    with pytest.raises(FileNotFoundError):
        ReplayCalendarSource(tmp_path / "missing.json", virtual_clock)


async def test_replay_calendar_must_be_a_list(tmp_path, virtual_clock):
    """JSON content must be a list, not an object."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError, match="JSON list"):
        ReplayCalendarSource(bad, virtual_clock)


async def test_replay_calendar_empty_list(tmp_path, virtual_clock):
    """An empty fixture returns no events."""
    cal_path = _write_cal(tmp_path / "empty.json", [])
    src = ReplayCalendarSource(cal_path, virtual_clock)
    assert await src.get_upcoming(3600) == []


# ---------------------------------------------------------------------------
# Factory smoke test
# ---------------------------------------------------------------------------


async def test_make_source_factory_replay_mode(monkeypatch, tmp_path, virtual_clock):
    """HELIOS_REPLAY=1 with fixture env vars yields a working ReplaySourceFactory."""
    from helios.sources import make_source_factory, ReplaySourceFactory

    mic = synth_wav(tmp_path / "mic.wav", duration_s=0.2)
    sysw = synth_wav(tmp_path / "sys.wav", duration_s=0.2)
    cal = _write_cal(tmp_path / "cal.json", [])

    monkeypatch.setenv("HELIOS_REPLAY", "1")
    monkeypatch.setenv("HELIOS_REPLAY_MIC_FIXTURE", str(mic))
    monkeypatch.setenv("HELIOS_REPLAY_SYSTEM_FIXTURE", str(sysw))
    monkeypatch.setenv("HELIOS_REPLAY_CAL_FIXTURE", str(cal))
    monkeypatch.setenv("HELIOS_REPLAY_SPEED", "20.0")

    factory = make_source_factory(config=None)
    assert isinstance(factory, ReplaySourceFactory)
    assert factory.speed_multiplier == 20.0

    q: asyncio.Queue[AudioSample] = asyncio.Queue()
    mic_src = factory.make_mic_source(q, virtual_clock, start_ts=virtual_clock.time())
    sys_src = factory.make_system_source(q, virtual_clock, start_ts=virtual_clock.time())
    cal_src = factory.make_calendar_source(virtual_clock)

    assert hasattr(mic_src, "start")
    assert hasattr(sys_src, "start")
    assert await cal_src.get_upcoming(60) == []


async def test_make_source_factory_real_mode_returns_real_factory(monkeypatch):
    """Default (no HELIOS_REPLAY) returns RealSourceFactory; calendar still raises."""
    from helios.sources import make_source_factory
    from helios.sources.real import MicSource, RealSourceFactory, SCKSystemAudioSource

    monkeypatch.delenv("HELIOS_REPLAY", raising=False)
    factory = make_source_factory(config=None)

    assert isinstance(factory, RealSourceFactory)
    # Constructing sources is hardware-free (no streams open until start()).
    mic = factory.make_mic_source(asyncio.Queue(), clock=None, start_ts=None)
    sys_ = factory.make_system_source(asyncio.Queue(), clock=None, start_ts=None)
    assert isinstance(mic, MicSource)
    assert isinstance(sys_, SCKSystemAudioSource)
    # Calendar is Phase 2.
    with pytest.raises(NotImplementedError, match="Phase 2"):
        factory.make_calendar_source(None)
