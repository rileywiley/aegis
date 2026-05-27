"""Tests for ``helios.capture.stream_manager.StreamManager`` (Track 1E.1).

Covers (per HELIOS_BUILD_PLAN.md):
- Happy path with two replay sources flowing through to a real chunker
- ``start()`` raises ``StreamStartError`` when mic init fails
- ``start()`` rolls back the mic source if system init fails
- Watchdog detects no-buffers-for-30s and triggers a restart
- ``stop()`` cleanly shuts both sources + watchdog (idempotent)
- Natural EOF is NOT treated as a stall (no spurious restart)
- ``on_stall`` callback fires on stall with correct channel + timing

Wake-notification test is intentionally deferred with Track 1G.

VirtualClock drives all timing. The pattern: start the manager, yield with
``_settle()`` to let tasks register their first sleep, then ``advance()``
the virtual clock past the watchdog threshold to trigger checks.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from helios.capture.chunker import Chunker
from helios.capture.stream_manager import (
    StreamManager,
    StreamStartError,
)
from helios.config import AudioConfig
from helios.db import queries
from helios.db.migrations import run_migrations
from helios.sources.interface import AudioSample
from helios.sources.replay import ReplayMicSource, ReplaySystemAudioSource
from tests.conftest import synth_wav


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audio_cfg() -> AudioConfig:
    return AudioConfig()


@pytest.fixture
async def migrated_db(tmp_db, migrations_dir):
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


@pytest.fixture
async def session_id(migrated_db):
    return await queries.create_session(
        migrated_db, kind="manual_screen", started_at=1712345000.0
    )


@pytest.fixture
def chunker(session_id, tmp_path, migrated_db, audio_cfg, virtual_clock):
    return Chunker(
        session_id=session_id,
        storage_root=tmp_path / "storage",
        db=migrated_db,
        config=audio_cfg,
        clock=virtual_clock,
    )


async def _settle(n: int = 8) -> None:
    """Yield to let newly-registered tasks reach their first await."""
    for _ in range(n):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Test doubles for failure / stall scenarios
# ---------------------------------------------------------------------------


class _FailingSource:
    """An AudioSource whose start() always raises."""

    is_running = False

    async def start(self) -> None:
        raise RuntimeError("init failed")

    async def stop(self) -> None:
        return None

    async def samples(self) -> AsyncIterator[AudioSample]:
        if False:  # pragma: no cover - never reached
            yield  # pragma: no cover

    # Marker-protocol stub for SystemAudioSource.
    def video_frames(self):  # pragma: no cover - unused
        raise NotImplementedError


class _StallingSource:
    """A controllable source that emits one sample then stalls indefinitely.

    Tracks ``start_calls`` so tests can assert that the watchdog triggered
    a restart. ``samples()`` yields one block on the first iteration of
    each lifecycle, then blocks on an event that is only set on stop().
    """

    def __init__(self, channel: str, clock) -> None:
        self._channel = channel
        self._clock = clock
        self.start_calls = 0
        self.stop_calls = 0
        self._stop_event: asyncio.Event | None = None
        self._emitted_first = False
        self.is_running = False

    async def start(self) -> None:
        self.start_calls += 1
        self._stop_event = asyncio.Event()
        # Re-arm so a restarted source emits one fresh sample again.
        self._emitted_first = False
        self.is_running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        if self._stop_event is not None:
            self._stop_event.set()
        self.is_running = False

    async def samples(self) -> AsyncIterator[AudioSample]:
        import numpy as np

        # Wait until start() has armed the stop event.
        assert self._stop_event is not None
        # Emit one short block (so chunker call path is exercised) then
        # block until stop().
        if not self._emitted_first:
            self._emitted_first = True
            yield AudioSample(
                channel=self._channel,  # type: ignore[arg-type]
                ts=self._clock.time(),
                samples=np.zeros(1600, dtype=np.int16),
            )
        await self._stop_event.wait()

    def video_frames(self):  # pragma: no cover - unused
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Happy path: replay sources flow through to chunker, stop is clean.
# ---------------------------------------------------------------------------


async def test_happy_path_replay_sources_flow_to_chunker(
    tmp_path, virtual_clock, chunker, migrated_db, session_id
):
    """Two 5s replay sources produce chunks for both channels."""
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=5.0, freq_hz=440.0)
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=5.0, freq_hz=880.0)
    mic_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    sys_q: asyncio.Queue[AudioSample] = asyncio.Queue()

    mic_src = ReplayMicSource(
        mic_wav,
        mic_q,
        virtual_clock,
        start_ts=virtual_clock.time(),
        speed_multiplier=10.0,
    )
    sys_src = ReplaySystemAudioSource(
        sys_wav,
        sys_q,
        virtual_clock,
        start_ts=virtual_clock.time(),
        speed_multiplier=10.0,
    )

    # Watchdog set well above the simulated session length so it doesn't
    # kick in for this test.
    mgr = StreamManager(
        mic_source=mic_src,
        system_source=sys_src,
        chunker=chunker,
        clock=virtual_clock,
        watchdog_seconds=60.0,
    )

    await mgr.start()
    assert mgr.is_running is True

    # Drive the replay forward. Each replay block sleeps 0.01s of virtual
    # time at speed=10x; 50 blocks = 0.5s. Advance well past that.
    await _settle()
    await virtual_clock.advance(2.0)
    await _settle()

    # Force partial flush so the buffered tail (5s < 30s chunk size) lands.
    await chunker.flush_partial(reason="test_complete")

    await mgr.stop()
    assert mgr.is_running is False

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    channels_seen = {r.channel for r in rows}
    assert "mic" in channels_seen
    assert "system" in channels_seen


# ---------------------------------------------------------------------------
# 2. start() raises StreamStartError if mic init fails.
# ---------------------------------------------------------------------------


async def test_start_raises_when_mic_init_fails(
    tmp_path, virtual_clock, chunker
):
    """A mic source whose start() raises causes StreamStartError + no leaks."""
    mic_src = _FailingSource()
    # System won't be touched because mic fails first.
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=0.5)
    sys_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    sys_src = ReplaySystemAudioSource(sys_wav, sys_q, virtual_clock)

    mgr = StreamManager(mic_src, sys_src, chunker, virtual_clock)

    with pytest.raises(StreamStartError) as excinfo:
        await mgr.start()
    assert excinfo.value.channel == "mic"
    assert isinstance(excinfo.value.original, RuntimeError)

    # No reader tasks left running.
    assert mgr._reader_tasks == {}
    assert mgr._watchdog_task is None
    assert mgr.is_running is False
    # System source was never even touched.
    assert sys_src.is_running is False


# ---------------------------------------------------------------------------
# 3. start() rolls back mic if system init fails.
# ---------------------------------------------------------------------------


async def test_start_rolls_back_mic_when_system_fails(
    tmp_path, virtual_clock, chunker
):
    """A real ReplayMicSource is stopped if the system source fails to start."""
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=2.0)
    mic_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    mic_src = ReplayMicSource(
        mic_wav, mic_q, virtual_clock, speed_multiplier=10.0
    )
    sys_src = _FailingSource()

    mgr = StreamManager(mic_src, sys_src, chunker, virtual_clock)

    with pytest.raises(StreamStartError) as excinfo:
        await mgr.start()
    assert excinfo.value.channel == "system"

    # Mic was started then rolled back -> not running anymore.
    await _settle()
    assert mic_src.is_running is False
    assert mgr.is_running is False
    assert mgr._reader_tasks == {}
    assert mgr._watchdog_task is None


# ---------------------------------------------------------------------------
# 4. Watchdog detects stall and triggers restart.
# ---------------------------------------------------------------------------


async def test_watchdog_detects_stall_and_restarts(
    tmp_path, virtual_clock, chunker
):
    """A source that emits once then stalls is restarted by the watchdog."""
    mic_src = _StallingSource("mic", virtual_clock)
    sys_src = _StallingSource("system", virtual_clock)

    mgr = StreamManager(
        mic_source=mic_src,
        system_source=sys_src,
        chunker=chunker,
        clock=virtual_clock,
        watchdog_seconds=0.5,  # short threshold for the test
    )

    await mgr.start()
    # Both sources started exactly once so far.
    assert mic_src.start_calls == 1
    assert sys_src.start_calls == 1

    # Let the readers consume their one block + register the wait.
    await _settle()

    # Advance well past the watchdog threshold + one check interval. The
    # check interval is watchdog_seconds/3 = ~0.167s; advancing 2s gives
    # the watchdog several opportunities to detect + restart.
    await virtual_clock.advance(2.0)
    await _settle()

    # Each source must have been restarted at least once.
    assert mic_src.start_calls >= 2, (
        f"expected mic restart, start_calls={mic_src.start_calls}"
    )
    assert sys_src.start_calls >= 2, (
        f"expected system restart, start_calls={sys_src.start_calls}"
    )

    await mgr.stop()


# ---------------------------------------------------------------------------
# 5. stop() is clean and idempotent.
# ---------------------------------------------------------------------------


async def test_stop_is_clean_and_idempotent(
    tmp_path, virtual_clock, chunker
):
    """stop() drains both sources + watchdog; second call is a no-op."""
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=1.0)
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=1.0)
    mic_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    sys_q: asyncio.Queue[AudioSample] = asyncio.Queue()

    mic_src = ReplayMicSource(
        mic_wav, mic_q, virtual_clock, speed_multiplier=10.0
    )
    sys_src = ReplaySystemAudioSource(
        sys_wav, sys_q, virtual_clock, speed_multiplier=10.0
    )

    mgr = StreamManager(
        mic_src, sys_src, chunker, virtual_clock, watchdog_seconds=60.0
    )

    await mgr.start()
    assert mgr.is_running is True

    await mgr.stop()
    assert mgr.is_running is False
    assert mic_src.is_running is False
    assert sys_src.is_running is False

    # Second stop must not raise.
    await mgr.stop()
    assert mgr.is_running is False


async def test_stop_before_start_is_safe(
    tmp_path, virtual_clock, chunker
):
    """Calling stop() on a never-started manager must not raise."""
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=0.2)
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=0.2)
    mic_src = ReplayMicSource(mic_wav, asyncio.Queue(), virtual_clock)
    sys_src = ReplaySystemAudioSource(sys_wav, asyncio.Queue(), virtual_clock)

    mgr = StreamManager(mic_src, sys_src, chunker, virtual_clock)
    # Should be a no-op, not raise.
    await mgr.stop()
    assert mgr.is_running is False


# ---------------------------------------------------------------------------
# 6. EOF distinguished from stall.
# ---------------------------------------------------------------------------


async def test_eof_does_not_trigger_restart(
    tmp_path, virtual_clock, chunker
):
    """A finite replay source that EOFs naturally is NOT restarted."""

    class CountingMic(ReplayMicSource):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.start_calls = 0

        async def start(self):
            self.start_calls += 1
            await super().start()

    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=0.5)
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=0.5)

    mic_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    sys_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    mic_src = CountingMic(
        mic_wav, mic_q, virtual_clock, speed_multiplier=10.0
    )
    sys_src = ReplaySystemAudioSource(
        sys_wav, sys_q, virtual_clock, speed_multiplier=10.0
    )

    mgr = StreamManager(
        mic_src, sys_src, chunker, virtual_clock, watchdog_seconds=0.5
    )
    await mgr.start()
    assert mic_src.start_calls == 1

    # Advance well past EOF + multiple watchdog windows.
    await _settle()
    await virtual_clock.advance(3.0)
    await _settle()

    # No restart attempts: EOF is intentional.
    assert mic_src.start_calls == 1, (
        f"EOF should not trigger restart, got start_calls={mic_src.start_calls}"
    )

    await mgr.stop()


# ---------------------------------------------------------------------------
# 7. on_stall callback fires when a stall is detected.
# ---------------------------------------------------------------------------


async def test_on_stall_callback_invoked(
    tmp_path, virtual_clock, chunker
):
    """The on_stall callback receives (channel, gap_start_ts, gap_end_ts, reason)."""
    mic_src = _StallingSource("mic", virtual_clock)
    sys_src = _StallingSource("system", virtual_clock)

    calls: list[tuple[str, float, float, str]] = []

    async def on_stall(
        channel: str, start: float, end: float, reason: str
    ) -> None:
        calls.append((channel, start, end, reason))

    mgr = StreamManager(
        mic_source=mic_src,
        system_source=sys_src,
        chunker=chunker,
        clock=virtual_clock,
        watchdog_seconds=0.5,
        on_stall=on_stall,
        power=_NoopPower(),
    )

    await mgr.start()
    await _settle()
    await virtual_clock.advance(2.0)
    await _settle()

    # At least one stall reported per channel.
    channels_reported = {c for c, _, _, _ in calls}
    assert "mic" in channels_reported
    assert "system" in channels_reported

    # Each call must have monotonically ordered timestamps and a known reason.
    for _ch, start, end, reason in calls:
        assert end > start
        assert reason in ("watchdog_stall", "system_sleep")

    # Watchdog-driven stalls (not wake events) must be labeled as such.
    assert all(r == "watchdog_stall" for _, _, _, r in calls)

    await mgr.stop()


# ---------------------------------------------------------------------------
# Track 1G — power assertion + wake handling.
# ---------------------------------------------------------------------------


class _RecordingPower:
    """Test double for PowerAssertion that records acquire/release order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.is_held = False

    async def acquire(self) -> bool:
        self.events.append("acquire")
        self.is_held = True
        return True

    async def release(self) -> None:
        self.events.append("release")
        self.is_held = False


class _NoopPower:
    """Test double that no-ops both calls (used by tests that don't care)."""

    is_held = False

    async def acquire(self) -> bool:
        return True

    async def release(self) -> None:
        return None


async def test_power_assertion_acquired_on_start_released_on_stop(
    tmp_path, virtual_clock, chunker
):
    """StreamManager must acquire the assertion on start and release on stop."""
    mic_src = _StallingSource("mic", virtual_clock)
    sys_src = _StallingSource("system", virtual_clock)
    power = _RecordingPower()

    mgr = StreamManager(
        mic_source=mic_src,
        system_source=sys_src,
        chunker=chunker,
        clock=virtual_clock,
        watchdog_seconds=10.0,
        power=power,
    )

    await mgr.start()
    assert power.events == ["acquire"]
    assert power.is_held is True

    await mgr.stop()
    assert power.events == ["acquire", "release"]
    assert power.is_held is False


async def test_handle_wake_labels_stalls_as_system_sleep(
    tmp_path, virtual_clock, chunker
):
    """A simulated wake event labels open gaps as system_sleep, not watchdog_stall."""
    mic_src = _StallingSource("mic", virtual_clock)
    sys_src = _StallingSource("system", virtual_clock)

    calls: list[tuple[str, float, float, str]] = []

    async def on_stall(
        channel: str, start: float, end: float, reason: str
    ) -> None:
        calls.append((channel, start, end, reason))

    mgr = StreamManager(
        mic_source=mic_src,
        system_source=sys_src,
        chunker=chunker,
        clock=virtual_clock,
        watchdog_seconds=10.0,
        on_stall=on_stall,
        power=_NoopPower(),
    )

    await mgr.start()
    await _settle()

    # Cancel the watchdog so it doesn't race the deliberate wake call —
    # in production, system sleep makes loop.time() jump and the watchdog
    # routes to _handle_wake; in tests we invoke _handle_wake directly.
    if mgr._watchdog_task is not None:
        mgr._watchdog_task.cancel()
        try:
            await mgr._watchdog_task
        except (asyncio.CancelledError, Exception):
            pass

    # Simulate a 120s system sleep.
    await virtual_clock.advance(120.0)
    await mgr._handle_wake(slept_seconds=120.0)
    await _settle()

    reasons = {r for _c, _s, _e, r in calls}
    assert reasons == {"system_sleep"}

    channels = {c for c, _s, _e, _r in calls}
    assert "mic" in channels
    assert "system" in channels

    await mgr.stop()


# ---------------------------------------------------------------------------
# Bonus: restart bound — exceeding cap logs degraded and stops trying.
# ---------------------------------------------------------------------------


async def test_restart_bound_gives_up_after_cap(
    tmp_path, virtual_clock, chunker
):
    """After 3 restart attempts in the rolling window, the watchdog gives up."""
    mic_src = _StallingSource("mic", virtual_clock)
    sys_src = _StallingSource("system", virtual_clock)

    mgr = StreamManager(
        mic_source=mic_src,
        system_source=sys_src,
        chunker=chunker,
        clock=virtual_clock,
        watchdog_seconds=0.5,
    )

    await mgr.start()
    await _settle()
    # Advance generously to give the watchdog many opportunities to attempt
    # and exceed the bound.
    for _ in range(8):
        await virtual_clock.advance(1.0)
        await _settle()

    # We expect at most 1 (initial) + 3 (cap) = 4 start calls per channel.
    assert mic_src.start_calls <= 4
    assert sys_src.start_calls <= 4
    # And the channel must be in the given-up set after the cap.
    assert "mic" in mgr._given_up
    assert "system" in mgr._given_up

    await mgr.stop()


# ---------------------------------------------------------------------------
# Bonus: double start is a no-op.
# ---------------------------------------------------------------------------


async def test_double_start_is_noop(
    tmp_path, virtual_clock, chunker
):
    """Calling start() while already running must not spawn duplicate tasks."""
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=1.0)
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=1.0)
    mic_src = ReplayMicSource(
        mic_wav, asyncio.Queue(), virtual_clock, speed_multiplier=10.0
    )
    sys_src = ReplaySystemAudioSource(
        sys_wav, asyncio.Queue(), virtual_clock, speed_multiplier=10.0
    )

    mgr = StreamManager(
        mic_src, sys_src, chunker, virtual_clock, watchdog_seconds=60.0
    )
    await mgr.start()
    first_readers = dict(mgr._reader_tasks)
    first_watchdog = mgr._watchdog_task

    await mgr.start()  # no-op
    assert mgr._reader_tasks == first_readers
    assert mgr._watchdog_task is first_watchdog

    await mgr.stop()


# ---------------------------------------------------------------------------
# Bonus: invalid watchdog_seconds rejected at construction.
# ---------------------------------------------------------------------------


def test_invalid_watchdog_seconds_rejected(
    tmp_path, virtual_clock, chunker
):
    """watchdog_seconds <= 0 must raise ValueError."""
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=0.2)
    sys_wav = synth_wav(tmp_path / "sys.wav", duration_s=0.2)
    mic_src = ReplayMicSource(mic_wav, asyncio.Queue(), virtual_clock)
    sys_src = ReplaySystemAudioSource(sys_wav, asyncio.Queue(), virtual_clock)

    with pytest.raises(ValueError, match="watchdog_seconds"):
        StreamManager(mic_src, sys_src, chunker, virtual_clock, watchdog_seconds=0)


# ---------------------------------------------------------------------------
# Wave 5b: voice-note sessions pass system_source=None so SCK never inits
# (and never probes Screen Recording permission).
# ---------------------------------------------------------------------------


async def test_mic_only_session_runs_without_system_source(
    tmp_path, virtual_clock, chunker, migrated_db, session_id
):
    """``StreamManager(system_source=None)`` runs mic-only cleanly.

    Voice-note kind sessions skip system audio so no Screen Recording
    prompt fires. The watchdog must not flag the absent system channel
    as stalled, stop() must not try to stop a None source, and the mic
    channel must still produce chunks.
    """
    mic_wav = synth_wav(tmp_path / "mic.wav", duration_s=2.0, freq_hz=440.0)
    mic_q: asyncio.Queue[AudioSample] = asyncio.Queue()
    mic_src = ReplayMicSource(
        mic_wav,
        mic_q,
        virtual_clock,
        start_ts=virtual_clock.time(),
        speed_multiplier=10.0,
    )

    mgr = StreamManager(
        mic_source=mic_src,
        system_source=None,
        chunker=chunker,
        clock=virtual_clock,
        watchdog_seconds=60.0,
    )

    await mgr.start()
    assert mgr.is_running is True

    await _settle()
    await virtual_clock.advance(1.0)
    await _settle()

    await chunker.flush_partial(reason="test_complete")

    await mgr.stop()
    assert mgr.is_running is False

    rows = await queries.get_session_audio_chunks(migrated_db, session_id)
    channels_seen = {r.channel for r in rows}
    assert "mic" in channels_seen
    assert "system" not in channels_seen, (
        "mic-only session must not produce system-channel chunks"
    )
