"""Source abstraction contracts for Helios capture.

Per HELIOS.md §8.1, all audio sources expose the same async-iterator surface
regardless of whether they are real (mic / system audio) or replay (WAV
fixtures driven by a VirtualClock). This file defines the protocols and the
shared data containers used across the capture pipeline.

Note: only replay implementations land in Phase 1. The real mic / SCK
sources arrive in a follow-up; the protocols here are the contract both
flavours must satisfy.
"""

from __future__ import annotations

from typing import AsyncIterator, Literal, NamedTuple, Protocol, runtime_checkable

import numpy as np
from pydantic import BaseModel, Field


class AudioSample(NamedTuple):
    """A single block of mono int16 PCM audio.

    `ts` is the UTC epoch timestamp (seconds, float) of the FIRST sample in
    the block; downstream consumers compute end-time as
    `ts + len(samples) / sample_rate`.
    """

    channel: Literal["mic", "system"]
    ts: float
    samples: np.ndarray  # mono int16, shape (N,)


class VideoFrame(NamedTuple):
    """A single screen capture frame, JPEG-encoded.

    Used by the OCR worker (§10) — not produced by Phase 1 replay sources.
    """

    ts: float
    jpeg: bytes


@runtime_checkable
class AudioSource(Protocol):
    """Common surface for any audio source (mic, system, replay)."""

    is_running: bool

    async def start(self) -> None:
        """Begin capture / replay. Idempotent: a second call while running is a no-op."""
        ...

    async def stop(self) -> None:
        """Stop capture and release resources. Idempotent."""
        ...

    def samples(self) -> AsyncIterator[AudioSample]:
        """Async iterator the consumer drains for emitted samples."""
        ...


@runtime_checkable
class SystemAudioSource(AudioSource, Protocol):
    """Marker protocol for system-audio sources.

    Implementations: SCKSystemAudioSource (real, Phase 1 follow-up),
    ReplaySystemAudioSource (this module). Optional video-frame surface for
    the OCR worker; replay implementations may stub it.
    """

    def video_frames(self) -> AsyncIterator[VideoFrame]:
        """Async iterator over screen-capture JPEG frames. Optional."""
        ...


class CalendarEvent(BaseModel):
    """Shape emitted by both replay and real calendar sources."""

    id: str
    title: str
    start_ts: float
    end_ts: float
    attendees: list[str] = Field(default_factory=list)


@runtime_checkable
class CalendarSource(Protocol):
    """Lookup of upcoming calendar events for the scheduler."""

    async def get_upcoming(self, horizon_seconds: int) -> list[CalendarEvent]:
        """Return events starting within the next `horizon_seconds`, sorted by start_ts."""
        ...
