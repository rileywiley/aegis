"""Replay sources for tests and dev: read fixture files, drive via Clock.

Per HELIOS_BUILD_PLAN.md Track 1B.3, replay sources read pre-recorded fixtures
and emit samples at a configurable speed against a Clock. Tests use
VirtualClock to advance time deterministically; the dev daemon can use
RealClock with `speed_multiplier=1.0` for wall-clock playback.

Real `MicSource` / `SCKSystemAudioSource` (sounddevice + Swift helper) live
in a follow-up; this file is replay-only.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path
from typing import AsyncIterator, Literal

import numpy as np

from helios.clock import Clock
from helios.log import get_logger
from helios.sources.interface import (
    AudioSample,
    CalendarEvent,
    VideoFrame,
)

_log = get_logger("sources.replay")

# WAV requirements enforced by replay sources.
_EXPECTED_SAMPLE_RATE = 16000
_EXPECTED_CHANNELS = 1
_EXPECTED_SAMPWIDTH = 2  # bytes => int16

# Sentinel pushed onto the iterator queue to signal clean termination.
_STOP_SENTINEL: object = object()


class _ReplayAudioSourceBase:
    """Shared replay implementation for mic + system channels.

    Owns the WAV reader, the emit task, and the async iterator queue. The
    public mic / system classes are thin wrappers that pin the channel label.
    """

    def __init__(
        self,
        wav_path: Path,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        channel: Literal["mic", "system"],
        start_ts: float | None = None,
        speed_multiplier: float = 10.0,
        blocksize: int = 1600,
    ) -> None:
        if speed_multiplier <= 0:
            raise ValueError(f"speed_multiplier must be > 0 (got {speed_multiplier})")
        if blocksize <= 0:
            raise ValueError(f"blocksize must be > 0 (got {blocksize})")

        self._wav_path = Path(wav_path)
        self._queue = queue
        self._clock = clock
        self._channel = channel
        self._start_ts_override = start_ts
        self._speed_multiplier = speed_multiplier
        self._blocksize = blocksize

        self._task: asyncio.Task | None = None
        # Internal queue feeds the public async-iterator returned from samples().
        self._iter_queue: asyncio.Queue[AudioSample | object] = asyncio.Queue()
        self._stopping = False

    # ------------------------------------------------------------------ public
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return

        # Validate WAV format up-front — synchronous, fast, raises ValueError.
        samples_int16 = self._read_wav()
        start_ts = (
            self._start_ts_override
            if self._start_ts_override is not None
            else self._clock.time()
        )

        self._stopping = False
        self._task = asyncio.create_task(
            self._emit_loop(samples_int16, start_ts),
            name=f"replay-{self._channel}",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.warning("replay_emit_task_error", channel=self._channel)
        self._task = None
        # Push sentinel so any in-flight samples() iterator terminates cleanly.
        await self._iter_queue.put(_STOP_SENTINEL)

    async def samples(self) -> AsyncIterator[AudioSample]:
        """Async iterator yielding emitted samples until stop() is called."""
        while True:
            item = await self._iter_queue.get()
            if item is _STOP_SENTINEL:
                return
            yield item  # type: ignore[misc]

    # ----------------------------------------------------------------- internal
    def _read_wav(self) -> np.ndarray:
        """Read and validate the fixture WAV. Returns mono int16 ndarray."""
        if not self._wav_path.exists():
            raise FileNotFoundError(f"replay WAV not found: {self._wav_path}")

        with wave.open(str(self._wav_path), "rb") as w:
            sample_rate = w.getframerate()
            n_channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            n_frames = w.getnframes()

            if sample_rate != _EXPECTED_SAMPLE_RATE:
                raise ValueError(
                    f"replay WAV {self._wav_path} has sample_rate={sample_rate}, "
                    f"expected {_EXPECTED_SAMPLE_RATE}"
                )
            if n_channels != _EXPECTED_CHANNELS:
                raise ValueError(
                    f"replay WAV {self._wav_path} has channels={n_channels}, "
                    f"expected {_EXPECTED_CHANNELS} (mono)"
                )
            if sampwidth != _EXPECTED_SAMPWIDTH:
                raise ValueError(
                    f"replay WAV {self._wav_path} has sampwidth={sampwidth} bytes, "
                    f"expected {_EXPECTED_SAMPWIDTH} (int16)"
                )

            raw = w.readframes(n_frames)

        return np.frombuffer(raw, dtype=np.int16)

    async def _emit_loop(self, samples_int16: np.ndarray, start_ts: float) -> None:
        """Push samples in fixed-size blocks aligned to clock progression."""
        block = self._blocksize
        sample_rate = _EXPECTED_SAMPLE_RATE
        block_seconds = block / sample_rate  # 0.1s for 1600 @ 16kHz
        sleep_per_block = block_seconds / self._speed_multiplier

        total = len(samples_int16)
        n_blocks = (total + block - 1) // block

        try:
            for i in range(n_blocks):
                if self._stopping:
                    break
                lo = i * block
                hi = min(lo + block, total)
                # `.copy()` so downstream owns its buffer (the underlying WAV
                # bytes are shared otherwise).
                chunk = samples_int16[lo:hi].copy()
                # ts uses real-time scale (NOT divided by speed_multiplier) so
                # downstream chunkers see realistic timestamps regardless of
                # playback speed.
                ts = start_ts + i * block_seconds
                sample = AudioSample(channel=self._channel, ts=ts, samples=chunk)
                # Only feed the iterator queue (consumed by samples()).
                # The external `queue` kwarg is retained for forward-compat
                # with the real source factory but is intentionally NOT
                # written to — the stream manager drains via samples().
                await self._iter_queue.put(sample)
                # Pace to clock; VirtualClock sleeps return only on advance().
                await self._clock.sleep(sleep_per_block)
        except asyncio.CancelledError:
            raise
        finally:
            # Signal the iterator to terminate when natural EOF is reached.
            await self._iter_queue.put(_STOP_SENTINEL)


class ReplayMicSource(_ReplayAudioSourceBase):
    """Mic-channel replay source. Reads a WAV and emits AudioSample(channel='mic')."""

    def __init__(
        self,
        wav_path: Path,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
        speed_multiplier: float = 10.0,
        blocksize: int = 1600,
    ) -> None:
        super().__init__(
            wav_path=wav_path,
            queue=queue,
            clock=clock,
            channel="mic",
            start_ts=start_ts,
            speed_multiplier=speed_multiplier,
            blocksize=blocksize,
        )


class ReplaySystemAudioSource(_ReplayAudioSourceBase):
    """System-audio-channel replay source.

    Same surface as ReplayMicSource but pins channel='system' and stubs the
    optional `video_frames()` surface from SystemAudioSource (OCR is not in
    Phase 1).
    """

    def __init__(
        self,
        wav_path: Path,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
        speed_multiplier: float = 10.0,
        blocksize: int = 1600,
    ) -> None:
        super().__init__(
            wav_path=wav_path,
            queue=queue,
            clock=clock,
            channel="system",
            start_ts=start_ts,
            speed_multiplier=speed_multiplier,
            blocksize=blocksize,
        )

    def video_frames(self) -> AsyncIterator[VideoFrame]:
        raise NotImplementedError(
            "video frames not implemented in Phase 1 replay"
        )


class ReplayCalendarSource:
    """Reads a JSON list of CalendarEvent dicts, filters by current clock time."""

    def __init__(self, json_path: Path, clock: Clock) -> None:
        self._json_path = Path(json_path)
        self._clock = clock
        self._events: list[CalendarEvent] = self._load()

    def _load(self) -> list[CalendarEvent]:
        if not self._json_path.exists():
            raise FileNotFoundError(
                f"replay calendar JSON not found: {self._json_path}"
            )
        raw = json.loads(self._json_path.read_text())
        if not isinstance(raw, list):
            raise ValueError(
                f"replay calendar {self._json_path} must contain a JSON list"
            )
        return [CalendarEvent.model_validate(item) for item in raw]

    async def get_upcoming(self, horizon_seconds: int) -> list[CalendarEvent]:
        """Return events whose window overlaps ``[now, now+horizon)``.

        Includes in-progress events (``start_ts < now`` but ``end_ts > now``)
        so a Phase-2 scheduler waking up mid-meeting can still discover and
        attach to it.
        """
        now = self._clock.time()
        cutoff = now + horizon_seconds
        upcoming = [
            e for e in self._events if e.end_ts > now and e.start_ts <= cutoff
        ]
        upcoming.sort(key=lambda e: e.start_ts)
        return upcoming
