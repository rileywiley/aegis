"""Source factory for Helios capture.

Selects between real (mic + SCK) and replay (fixture-driven) sources at
daemon startup. Selection is driven by the `HELIOS_REPLAY` env var:

- `HELIOS_REPLAY=1` → ReplaySourceFactory reads fixture WAVs / JSON
- otherwise → RealSourceFactory (sounddevice mic + Swift helper system audio)

Per HELIOS_BUILD_PLAN.md Track 1B.3, switching modes is a one-line change.

The real-mode ``RealSourceFactory.make_calendar_source`` intentionally
raises NotImplementedError — in Phase 2 the daemon's lifespan
instantiates :class:`helios.scheduler.calendar.CalendarClient` directly
(Aegis HTTP client) instead of routing through the source factory. Only
the replay path uses ``make_calendar_source`` (under HELIOS_REPLAY=1)
to load a fixture-driven :class:`ReplayCalendarSource`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Protocol

from helios.clock import Clock
from helios.sources.interface import (
    AudioSample,
    AudioSource,
    CalendarSource,
    SystemAudioSource,
)


class SourceFactory(Protocol):
    """Protocol every source factory implements."""

    def make_mic_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
    ) -> AudioSource: ...

    def make_system_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
    ) -> SystemAudioSource: ...

    def make_calendar_source(self, clock: Clock) -> CalendarSource: ...


class ReplaySourceFactory:
    """Produces replay sources from fixture file paths.

    Fixture paths are resolved up-front (constructor) so missing files fail
    early at daemon startup rather than mid-capture.
    """

    def __init__(
        self,
        mic_wav_path: Path,
        system_wav_path: Path,
        calendar_json_path: Path,
        speed_multiplier: float = 10.0,
    ) -> None:
        self.mic_wav_path = Path(mic_wav_path)
        self.system_wav_path = Path(system_wav_path)
        self.calendar_json_path = Path(calendar_json_path)
        self.speed_multiplier = speed_multiplier

    def make_mic_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
    ) -> AudioSource:
        from helios.sources.replay import ReplayMicSource

        return ReplayMicSource(
            wav_path=self.mic_wav_path,
            queue=queue,
            clock=clock,
            start_ts=start_ts,
            speed_multiplier=self.speed_multiplier,
        )

    def make_system_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
    ) -> SystemAudioSource:
        from helios.sources.replay import ReplaySystemAudioSource

        return ReplaySystemAudioSource(
            wav_path=self.system_wav_path,
            queue=queue,
            clock=clock,
            start_ts=start_ts,
            speed_multiplier=self.speed_multiplier,
        )

    def make_calendar_source(self, clock: Clock) -> CalendarSource:
        from helios.sources.replay import ReplayCalendarSource

        return ReplayCalendarSource(json_path=self.calendar_json_path, clock=clock)


def make_source_factory(config) -> SourceFactory:
    """Return the appropriate source factory based on HELIOS_REPLAY env var.

    `config` is the loaded HeliosConfig; the real factory uses
    `config.audio.sample_rate`. The replay factory currently ignores it
    (paths come from env vars to keep test setup self-contained).
    """
    if os.environ.get("HELIOS_REPLAY") == "1":
        mic = Path(
            os.environ.get(
                "HELIOS_REPLAY_MIC_FIXTURE",
                "tests/fixtures/audio/synth_mic_60s.wav",
            )
        )
        sys_ = Path(
            os.environ.get(
                "HELIOS_REPLAY_SYSTEM_FIXTURE",
                "tests/fixtures/audio/synth_system_60s.wav",
            )
        )
        cal = Path(
            os.environ.get(
                "HELIOS_REPLAY_CAL_FIXTURE",
                "tests/fixtures/calendar/empty.json",
            )
        )
        speed = float(os.environ.get("HELIOS_REPLAY_SPEED", "10.0"))
        return ReplaySourceFactory(mic, sys_, cal, speed)
    from helios.sources.real import RealSourceFactory

    samplerate = getattr(getattr(config, "audio", None), "sample_rate", 16000)
    enable_video = getattr(getattr(config, "ocr", None), "enabled", False)
    return RealSourceFactory(samplerate=samplerate, enable_video=enable_video)


__all__ = [
    "SourceFactory",
    "ReplaySourceFactory",
    "make_source_factory",
]
