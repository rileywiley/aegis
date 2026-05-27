"""Stream manager — owns mic + system audio sources for a capture session.

Per HELIOS.md §8.4 + Track 1G, this module:

* brings up both audio sources in a defined order,
* acquires a macOS idle-sleep power assertion (caffeinate) for the
  duration of the session and releases it on stop,
* spawns one reader task per channel that funnels samples into a single
  shared :class:`~helios.capture.chunker.Chunker`,
* runs a watchdog that distinguishes natural EOF from real stalls,
  detects system sleep via wall-clock jump, attempts a bounded number
  of restarts, and
* surfaces stalls to the orchestrator via an optional ``on_stall``
  callback with a reason discriminator (``"watchdog_stall"`` vs
  ``"system_sleep"``) so the orchestrator can write the right
  ``unavailable_reason`` (the orchestrator owns DB writes; the stream
  manager stays source/sink-only).

The manager is driven by the injected :class:`~helios.clock.Clock` for
restart bookkeeping and per-channel stall tracking. Wake detection uses
``loop.time()`` (the asyncio loop's monotonic clock, which advances
across system sleep) — this is intentionally outside the injected clock
so it works under both ``RealClock`` (production) and ``VirtualClock``
(tests, where wake events are exercised by directly calling
:meth:`StreamManager._handle_wake`).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Literal

from helios.capture._power import PowerAssertion
from helios.capture.chunker import Chunker
from helios.clock import Clock
from helios.log import get_logger
from helios.sources.interface import AudioSource, SystemAudioSource

log = get_logger("stream_manager")

Channel = Literal["mic", "system"]
_CHANNELS: tuple[Channel, ...] = ("mic", "system")

# Restart bound: at most _RESTART_MAX_ATTEMPTS within _RESTART_WINDOW_SECONDS
# per channel before we give up and log a degraded event.
_RESTART_MAX_ATTEMPTS = 3
_RESTART_WINDOW_SECONDS = 300.0  # 5 minutes

# Wake detection: if the watchdog's sleep call took more than this multiple
# of the requested interval (measured against asyncio loop.time(), which
# advances across system sleep), assume the system slept during the wait.
_WAKE_DETECT_MULTIPLIER = 3.0

# Stall-reason discriminator passed to OnStallCallback. Mirrors the values
# the orchestrator writes to ``audio_chunks.unavailable_reason``.
StallReason = Literal["watchdog_stall", "system_sleep"]


# Type alias for the on_stall callback. Signature:
# (channel, gap_start_ts, gap_end_ts, reason) -> Awaitable[None].
# ``gap_start_ts`` is the last observed sample timestamp (or session start
# if none seen yet); ``gap_end_ts`` is the clock time at which the gap was
# detected; ``reason`` discriminates watchdog stall vs system sleep so the
# orchestrator can label the chunk row.
OnStallCallback = Callable[[str, float, float, str], Awaitable[None]]


class StreamStartError(Exception):
    """Raised when ``StreamManager.start()`` fails to bring up both sources."""

    def __init__(self, channel: str, original: BaseException) -> None:
        super().__init__(f"failed to start {channel} source: {original!r}")
        self.channel = channel
        self.original = original


class StreamManager:
    """Owns mic + system audio sources for a single capture session.

    Concurrent reader tasks forward samples from each source's ``samples()``
    iterator to the shared chunker. A watchdog detects stalls (no samples
    for ``watchdog_seconds``) and attempts a bounded number of restarts
    per channel.

    Construction is pure dependency injection — the manager never imports
    the source factory or the DB layer. The orchestrator is responsible
    for wiring concrete sources, the chunker, and the clock.
    """

    def __init__(
        self,
        mic_source: AudioSource,
        system_source: SystemAudioSource | None,
        chunker: Chunker,
        clock: Clock,
        watchdog_seconds: float = 30.0,
        on_stall: OnStallCallback | None = None,
        power: PowerAssertion | None = None,
    ) -> None:
        if watchdog_seconds <= 0:
            raise ValueError(
                f"watchdog_seconds must be > 0 (got {watchdog_seconds})"
            )

        # ``system_source`` may be None for mic-only sessions (voice
        # notes per HELIOS.md §16.12). When absent, we run with only the
        # mic channel and skip every system-channel code path
        # (start/stop, reader task, watchdog tracking, restart attempts).
        self._sources: dict[Channel, AudioSource] = {"mic": mic_source}
        if system_source is not None:
            self._sources["system"] = system_source
        self._channels: tuple[Channel, ...] = tuple(self._sources.keys())  # type: ignore[assignment]
        self._chunker = chunker
        self._clock = clock
        self._watchdog_seconds = watchdog_seconds
        self._on_stall = on_stall
        # Power assertion: created on demand if not injected, so tests can
        # pass a no-op stub or a mock to verify acquire/release calls.
        self._power = power if power is not None else PowerAssertion()

        # Reader/watchdog task handles, owned for clean shutdown.
        self._reader_tasks: dict[Channel, asyncio.Task] = {}
        self._watchdog_task: asyncio.Task | None = None

        # Per-channel last-seen sample timestamp (clock.time() value, NOT
        # the audio sample's UTC ts). Used by the watchdog.
        self._last_sample_ts: dict[Channel, float] = {
            c: 0.0 for c in self._channels
        }

        # Per-channel restart bookkeeping for the bounded-attempts policy.
        self._restart_attempts: dict[Channel, list[float]] = {
            c: [] for c in self._channels
        }
        # Channels that have hit the restart cap; the watchdog leaves them
        # alone after logging the degraded event.
        self._given_up: set[Channel] = set()
        # Channels whose source has cleanly EOF'd; watchdog must NOT treat
        # an EOF'd channel as stalled.
        self._eof: set[Channel] = set()

        self._started = False
        self._stopping = False

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True between a successful ``start()`` and the matching ``stop()``.

        Note: this reflects manager state, not source aliveness — sources
        may EOF naturally while the manager is still considered running
        (the orchestrator owns session-end semantics).
        """
        return self._started and not self._stopping

    @property
    def degraded_channels(self) -> frozenset[str]:
        """Channels that have exhausted their restart budget.

        The orchestrator (and the eventual menu-bar UI) reads this to
        surface a "stream X is degraded — no further restart attempts"
        signal to the user. Returned as a ``frozenset`` to make it
        explicitly read-only.
        """
        return frozenset(self._given_up)

    async def start(self) -> None:
        """Start both sources, then spawn reader + watchdog tasks.

        Mic comes up first; if system source startup raises, the mic source
        is rolled back and a :class:`StreamStartError` is raised. Reader
        and watchdog tasks are only spawned after BOTH sources are running,
        so a failed startup leaves no orphan tasks behind.
        """
        if self._started:
            return

        # 1) Bring up mic first. Failure here is terminal; nothing to roll back.
        try:
            await self._sources["mic"].start()
        except Exception as e:
            log.error("mic_start_failed", error=str(e))
            raise StreamStartError("mic", e) from e

        # 2) Bring up system if present (None for mic-only voice-note
        # sessions). On failure, roll back mic before re-raising.
        if "system" in self._sources:
            try:
                await self._sources["system"].start()
            except Exception as e:
                log.error("system_start_failed", error=str(e))
                try:
                    await self._sources["mic"].stop()
                except Exception as rollback_exc:  # pragma: no cover - defensive
                    log.warning(
                        "mic_rollback_failed",
                        error=str(rollback_exc),
                    )
                raise StreamStartError("system", e) from e

        # 3) Initialize last-sample-ts so the watchdog doesn't immediately
        # fire on a fresh start. Then spawn reader tasks.
        now = self._clock.time()
        for ch in self._channels:
            self._last_sample_ts[ch] = now
        for ch in self._channels:
            self._reader_tasks[ch] = asyncio.create_task(
                self._reader_loop(ch),
                name=f"stream-reader-{ch}",
            )

        # 4) Spawn watchdog last — readers are already wired up, so any
        # immediate stall after start would be a real problem.
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(),
            name="stream-watchdog",
        )

        # 5) Acquire idle-sleep power assertion (best-effort, never blocks
        # startup if caffeinate is unavailable).
        try:
            await self._power.acquire()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("power_assertion_acquire_unexpected", error=str(e))

        self._started = True
        log.info("stream_manager_started", watchdog_seconds=self._watchdog_seconds)

    async def stop(self) -> None:
        """Cancel watchdog + readers, stop both sources, and reset state.

        Idempotent — calling stop() twice is safe.
        """
        if not self._started and not self._stopping:
            # Never started — nothing to clean up.
            return

        self._stopping = True

        # 1) Cancel the watchdog first so it doesn't fire mid-shutdown.
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None

        # 2) Cancel all reader tasks. We let them cancel cooperatively;
        # asyncio.gather with return_exceptions soaks up CancelledError.
        for task in self._reader_tasks.values():
            task.cancel()
        if self._reader_tasks:
            await asyncio.gather(
                *self._reader_tasks.values(),
                return_exceptions=True,
            )
        self._reader_tasks.clear()

        # 3) Stop all configured sources. ``_stop_source_safely``
        # already logs and swallows any per-source failure, so we can
        # fire-and-forget the gather here. Use return_exceptions
        # defensively in case a future change makes
        # ``_stop_source_safely`` raise. ``self._channels`` excludes
        # ``"system"`` for mic-only voice-note sessions.
        await asyncio.gather(
            *(self._stop_source_safely(ch) for ch in self._channels),
            return_exceptions=True,
        )

        # 4) Release the idle-sleep power assertion. Best-effort.
        try:
            await self._power.release()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("power_assertion_release_unexpected", error=str(e))

        self._started = False
        self._stopping = False
        log.info("stream_manager_stopped")

    # ------------------------------------------------------------------
    # Reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self, channel: Channel) -> None:
        """Drain the source's ``samples()`` iterator into the chunker.

        Exits cleanly on EOF (StopAsyncIteration) or task cancellation.
        On unexpected exceptions, logs the error and exits — the watchdog
        will notice the stalled stream and attempt restart.
        """
        source = self._sources[channel]
        try:
            async for sample in source.samples():
                if self._stopping:
                    return
                # Forward to the chunker. The Chunker is NOT safe for
                # concurrent add_samples on the same channel — but we only
                # have ONE reader per channel, so this is fine.
                try:
                    await self._chunker.add_samples(
                        sample.channel, sample.samples, sample.ts
                    )
                except Exception as e:  # pragma: no cover - defensive
                    log.warning(
                        "chunker_add_samples_failed",
                        channel=channel,
                        error=str(e),
                    )
                # Stamp the wall-clock (per the injected Clock) so the
                # watchdog can detect stalls in clock time.
                self._last_sample_ts[channel] = self._clock.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.warning("reader_loop_error", channel=channel, error=str(e))
            return

        # Natural EOF. Mark the channel as EOF so the watchdog leaves it
        # alone (no restart attempt — EOF is intentional, e.g. a finite
        # replay fixture).
        self._eof.add(channel)
        log.info("stream_eof", channel=channel)

    # ------------------------------------------------------------------
    # Watchdog loop
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Periodically check each channel for stalls and request restart.

        Sleeps via the injected clock so VirtualClock-driven tests can
        deterministically advance to trigger checks. Sleep interval is
        ``watchdog_seconds / 3`` so we react within ~1/3 of the threshold.

        Wake detection: measures elapsed wall time across each sleep
        against ``loop.time()`` (the asyncio loop's monotonic clock,
        which advances across system sleep). If the sleep took
        substantially longer than requested, the system likely slept —
        relabel via :meth:`_handle_wake` so all gaps in this iteration
        are tagged ``system_sleep`` rather than ``watchdog_stall``.
        """
        loop = asyncio.get_running_loop()
        sleep_interval = max(self._watchdog_seconds / 3.0, 0.05)
        try:
            while not self._stopping:
                pre_loop_t = loop.time()
                await self._clock.sleep(sleep_interval)
                if self._stopping:
                    return
                wall_elapsed = loop.time() - pre_loop_t
                if wall_elapsed > sleep_interval * _WAKE_DETECT_MULTIPLIER:
                    await self._handle_wake(wall_elapsed)
                else:
                    await self._check_for_stalls()
        except asyncio.CancelledError:
            raise

    async def _handle_wake(self, slept_seconds: float) -> None:
        """React to a detected system-sleep event.

        Marks all running channels' open gaps as ``system_sleep`` (so the
        orchestrator writes the right ``unavailable_reason``) and force-
        restarts each, regardless of the watchdog threshold. Resets each
        channel's ``_last_sample_ts`` to ``now`` so a subsequent watchdog
        tick doesn't re-report the same gap as ``watchdog_stall``.
        """
        log.warning("system_sleep_detected", slept_seconds=round(slept_seconds, 3))
        now = self._clock.time()
        for ch in self._channels:
            if ch in self._eof or ch in self._given_up:
                continue
            await self._fire_on_stall(ch, self._last_sample_ts[ch], now, "system_sleep")
            # Reset BEFORE attempting restart so even if restart fails, the
            # watchdog won't re-report this same gap on its next tick.
            self._last_sample_ts[ch] = now
            await self._attempt_restart(ch, now)

    async def _check_for_stalls(self) -> None:
        """Inspect each channel; restart any that stalled past the threshold."""
        now = self._clock.time()
        for ch in self._channels:
            if ch in self._eof:
                # Natural EOF — never treat as a stall.
                continue
            if ch in self._given_up:
                # Already exceeded restart cap; orchestrator should react
                # to the degraded event we already logged.
                continue
            gap = now - self._last_sample_ts[ch]
            if gap <= self._watchdog_seconds:
                continue

            gap_start = self._last_sample_ts[ch]
            gap_end = now
            log.warning(
                "stream_stalled",
                channel=ch,
                gap_seconds=round(gap, 3),
                threshold_seconds=self._watchdog_seconds,
            )

            # Notify orchestrator (it owns mark_chunk_unavailable). We
            # deliberately do this BEFORE attempting restart so the gap
            # row is recorded even if restart later fails too.
            await self._fire_on_stall(ch, gap_start, gap_end, "watchdog_stall")
            await self._attempt_restart(ch, now)

    async def _fire_on_stall(
        self,
        channel: Channel,
        gap_start: float,
        gap_end: float,
        reason: str,
    ) -> None:
        """Best-effort dispatch to the orchestrator's on_stall callback."""
        if self._on_stall is None:
            return
        try:
            await self._on_stall(channel, gap_start, gap_end, reason)
        except Exception as e:  # pragma: no cover - defensive
            log.warning(
                "on_stall_callback_failed",
                channel=channel,
                reason=reason,
                error=str(e),
            )

    async def _attempt_restart(self, channel: Channel, now: float) -> None:
        """Try to restart a stalled channel within the bounded-attempts policy."""
        # Prune old restart timestamps outside the rolling window.
        window_start = now - _RESTART_WINDOW_SECONDS
        self._restart_attempts[channel] = [
            t for t in self._restart_attempts[channel] if t >= window_start
        ]

        if len(self._restart_attempts[channel]) >= _RESTART_MAX_ATTEMPTS:
            log.error(
                "stream_degraded_giving_up",
                channel=channel,
                attempts=len(self._restart_attempts[channel]),
                window_seconds=_RESTART_WINDOW_SECONDS,
            )
            self._given_up.add(channel)
            return

        # Record this attempt and execute it.
        self._restart_attempts[channel].append(now)
        attempt_n = len(self._restart_attempts[channel])
        log.info(
            "stream_restart_attempt",
            channel=channel,
            attempt=attempt_n,
            max_attempts=_RESTART_MAX_ATTEMPTS,
        )

        # Cancel and replace the existing reader.
        old_reader = self._reader_tasks.get(channel)
        if old_reader is not None and not old_reader.done():
            old_reader.cancel()
            try:
                await old_reader
            except (asyncio.CancelledError, Exception):
                pass

        # Stop the source (best-effort), then bring it back up.
        try:
            await self._sources[channel].stop()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("restart_stop_failed", channel=channel, error=str(e))

        try:
            await self._sources[channel].start()
        except Exception as e:
            log.error("restart_start_failed", channel=channel, error=str(e))
            return

        # Reset the last-seen timestamp + spawn a fresh reader.
        self._last_sample_ts[channel] = self._clock.time()
        self._reader_tasks[channel] = asyncio.create_task(
            self._reader_loop(channel),
            name=f"stream-reader-{channel}",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _stop_source_safely(self, channel: Channel) -> None:
        """Stop a source, logging (not raising) on failure."""
        try:
            await self._sources[channel].stop()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("source_stop_failed", channel=channel, error=str(e))
