"""Per-channel audio chunker.

Buffers int16 PCM samples from a single capture session into 30-second
WAV files (one per channel) and writes a row to ``audio_chunks`` for each
flushed chunk. Silent buffers (RMS below the configured dB threshold) are
recorded as ``status='no_audio'`` rows with ``path = NULL`` — no WAV is
written for them, but the time range is still tracked so coverage stays
accurate.

Per the architectural rules, this module contains NO raw SQL — every DB
write goes through ``helios.db.queries``. Blocking WAV writes are
offloaded to the default executor so the event loop is never blocked.

See HELIOS.md §8.5 for the spec and §18.3 for the no-PII logging rule.
"""

from __future__ import annotations

import asyncio
import math
import wave
from pathlib import Path
from typing import Literal

import aiosqlite
import numpy as np

from helios.clock import Clock
from helios.config import AudioConfig
from helios.db import queries
from helios.log import get_logger

log = get_logger("chunker")

Channel = Literal["mic", "system"]
_CHANNELS: tuple[Channel, ...] = ("mic", "system")


class Chunker:
    """Buffers per-channel samples into 30-second WAV chunks + DB rows.

    Each channel maintains its own buffer and monotonic chunk index; channels
    flush independently. WAV files are written under
    ``<storage_root>/<YYYY-MM-DD>/<channel>/<session_id>_<chunk_index>.wav``
    using the local date from the injected ``Clock``.
    """

    def __init__(
        self,
        session_id: int,
        storage_root: Path,
        db: aiosqlite.Connection,
        config: AudioConfig,
        clock: Clock,
    ) -> None:
        self._session_id = session_id
        self._root = Path(storage_root)
        self._db = db
        self._config = config
        self._clock = clock

        self._sample_rate = config.sample_rate
        self._chunk_samples = config.sample_rate * config.chunk_seconds
        # silence_threshold_db is in dBFS relative to int16 full scale (32768).
        # rms_threshold = 10 ** (db / 20) * 32768
        self._silence_rms = (10.0 ** (config.silence_threshold_db / 20.0)) * 32768.0

        # Per-channel state. Buffers store np.ndarray int16 blocks; we
        # concatenate at flush time so we don't allocate continuously.
        self._buffers: dict[Channel, list[np.ndarray]] = {c: [] for c in _CHANNELS}
        self._buffer_samples: dict[Channel, int] = {c: 0 for c in _CHANNELS}
        self._buffer_starts: dict[Channel, float | None] = {c: None for c in _CHANNELS}
        self._chunk_index: dict[Channel, int] = {c: 0 for c in _CHANNELS}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_samples(
        self,
        channel: Channel,
        samples: np.ndarray,
        ts: float,
    ) -> None:
        """Append a block of samples to ``channel``'s buffer.

        ``ts`` is the UTC epoch timestamp of the FIRST sample in the block.
        Only the timestamp of the very first block in a buffer is retained
        (subsequent timestamps are derived from sample counts on flush).

        When the buffer reaches ``chunk_seconds * sample_rate`` samples,
        a full chunk is flushed (WAV written + DB row inserted). Any
        excess samples carry over to the next buffer with the timestamp
        derived from where the previous chunk ended.
        """
        if channel not in _CHANNELS:
            raise ValueError(f"unknown channel: {channel!r}")
        if samples.size == 0:
            return

        # Ensure int16; downstream WAV writer assumes sampwidth=2.
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16, copy=False)

        if self._buffer_starts[channel] is None:
            self._buffer_starts[channel] = ts

        self._buffers[channel].append(samples)
        self._buffer_samples[channel] += int(samples.shape[0])

        # A single add could push us across the chunk boundary by more
        # than one chunk's worth — flush in a loop just in case.
        while self._buffer_samples[channel] >= self._chunk_samples:
            await self._flush_full(channel)

    async def flush_partial(self, reason: str = "session_stop") -> None:
        """Flush whatever remains in each channel's buffer as a partial chunk.

        Empty buffers (zero samples accumulated) are skipped. Silent
        buffers are still flushed — they get a ``no_audio`` row so the
        time range remains tracked.
        """
        for channel in _CHANNELS:
            if self._buffer_samples[channel] == 0:
                continue
            await self._flush(channel, partial=True, reason=reason)

    # ------------------------------------------------------------------
    # Internal flush helpers
    # ------------------------------------------------------------------

    async def _flush_full(self, channel: Channel) -> None:
        """Emit exactly one full chunk; carry the remainder forward."""
        # Concatenate everything we have, then split off exactly one chunk.
        all_samples = np.concatenate(self._buffers[channel])
        full = all_samples[: self._chunk_samples]
        leftover = all_samples[self._chunk_samples :]
        start_ts = self._buffer_starts[channel]
        assert start_ts is not None  # guaranteed by add_samples

        await self._emit(channel, full, start_ts, partial=False)

        # Reset buffer; if there's leftover, it becomes the new buffer
        # and its start_ts is start_ts + duration_of_full.
        if leftover.size > 0:
            new_start = start_ts + (full.shape[0] / self._sample_rate)
            self._buffers[channel] = [leftover]
            self._buffer_samples[channel] = int(leftover.shape[0])
            self._buffer_starts[channel] = new_start
        else:
            self._buffers[channel] = []
            self._buffer_samples[channel] = 0
            self._buffer_starts[channel] = None

    async def _flush(
        self,
        channel: Channel,
        partial: bool,
        reason: str | None = None,
    ) -> None:
        """Flush whatever's currently buffered for ``channel`` as a single chunk."""
        data = np.concatenate(self._buffers[channel])
        start_ts = self._buffer_starts[channel]
        assert start_ts is not None

        await self._emit(channel, data, start_ts, partial=partial, reason=reason)

        self._buffers[channel] = []
        self._buffer_samples[channel] = 0
        self._buffer_starts[channel] = None

    async def _emit(
        self,
        channel: Channel,
        data: np.ndarray,
        start_ts: float,
        partial: bool,
        reason: str | None = None,
    ) -> None:
        """Write a chunk to disk + DB (or just DB for silent chunks)."""
        sample_count = int(data.shape[0])
        end_ts = start_ts + (sample_count / self._sample_rate)

        if self._is_silent(data):
            chunk_id = await queries.insert_no_audio_chunk(
                self._db,
                session_id=self._session_id,
                channel=channel,
                start_ts=start_ts,
                end_ts=end_ts,
                samples=sample_count,
                partial=partial,
            )
            # Burn an index slot even for silent chunks so per-channel
            # filenames stay strictly monotonic across mixed flushes.
            self._chunk_index[channel] += 1
            log.info(
                "chunk_no_audio",
                session_id=self._session_id,
                channel=channel,
                chunk_id=chunk_id,
                samples=sample_count,
                partial=partial,
                reason=reason,
            )
            return

        index = self._chunk_index[channel]
        path = self._build_path(channel, index)
        path.parent.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, _write_wav_blocking, path, data, self._sample_rate
        )

        chunk_id = await queries.insert_audio_chunk(
            self._db,
            session_id=self._session_id,
            channel=channel,
            start_ts=start_ts,
            end_ts=end_ts,
            path=str(path),
            samples=sample_count,
            partial=partial,
        )
        self._chunk_index[channel] += 1
        log.info(
            "chunk_recorded",
            session_id=self._session_id,
            channel=channel,
            chunk_id=chunk_id,
            samples=sample_count,
            partial=partial,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_silent(self, data: np.ndarray) -> bool:
        """RMS-based silence check against the configured dBFS threshold."""
        if data.size == 0:
            return True
        # float32 to avoid int16 overflow when squaring.
        f = data.astype(np.float32)
        rms = math.sqrt(float(np.mean(f * f)))
        return rms < self._silence_rms

    def _build_path(self, channel: Channel, index: int) -> Path:
        """``<root>/<YYYY-MM-DD>/<channel>/<session_id>_<index:05d>.wav``"""
        day = self._clock.now_local().strftime("%Y-%m-%d")
        return (
            self._root
            / day
            / channel
            / f"{self._session_id}_{index:05d}.wav"
        )


def _write_wav_blocking(path: Path, data: np.ndarray, sample_rate: int) -> None:
    """Write mono int16 PCM WAV. Called in the default executor."""
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(data.tobytes())
