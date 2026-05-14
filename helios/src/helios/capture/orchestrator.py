"""Capture session orchestrator.

Per HELIOS_BUILD_PLAN.md Track 1F.1, the ``CaptureOrchestrator`` sits above
the :class:`~helios.capture.stream_manager.StreamManager` and owns:

* session row lifecycle (``capture_sessions`` insert + end),
* calendar-event linkage (``session_calendar_links``),
* the per-session :class:`~helios.capture.chunker.Chunker`,
* the stream manager (sources + watchdog), and
* crash recovery on daemon startup.

Phase-1 simplification: **only one active session at a time.** The build
plan describes adjacent-session stream reuse (so two back-to-back calendar
sessions don't restart sources between them) — that's an optimisation for
Phase 2's calendar-driven scheduling. For the Phase-1 manual API slice
(``POST /v1/capture/{start,stop}``) and the smoke test, single-active is
the correct shape and simplifies failure handling. Attempting to start a
second session while one is active raises ``RuntimeError`` so the API
layer can surface a 409.

Per the Wave 4F coordination notes:

* No raw SQL outside :mod:`helios.db.queries` — every DB write goes through
  a query helper.
* The watchdog stall callback writes a fresh ``audio_chunks`` row with
  ``status='unavailable'`` and ``unavailable_reason='watchdog_stall'`` via
  :func:`helios.db.queries.insert_unavailable_chunk`.
* No PII in logs: only IDs, kinds, durations, channels.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import aiosqlite

from helios.capture.chunker import Chunker
from helios.capture.stream_manager import StreamManager
from helios.clock import Clock
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.connection import DatabasePool
from helios.log import get_logger
from helios.sources import SourceFactory
from helios.sources.interface import AudioSample, CalendarEvent

log = get_logger("orchestrator")


SessionKind = Literal["calendar", "continuous", "manual_screen", "voice_note"]


class CaptureOrchestrator:
    """Coordinates session lifecycle: DB rows, chunker, stream manager.

    One active session at a time. Construct once at daemon startup; call
    :meth:`recover` after migrations have run. Each ``start_session`` call
    builds a fresh chunker + stream manager pair using the injected
    source factory; ``stop_session`` tears them both down.
    """

    def __init__(
        self,
        db_pool: DatabasePool,
        config: HeliosConfig,
        clock: Clock,
        source_factory: SourceFactory,
    ) -> None:
        self._db_pool = db_pool
        self._config = config
        self._clock = clock
        self._factory = source_factory

        self._storage_root = Path(config.storage.root).expanduser()

        # Active-session state. None ⇔ no session running.
        self._active_session_id: int | None = None
        self._active_chunker: Chunker | None = None
        self._active_stream_manager: StreamManager | None = None

        # Serialise start/stop so concurrent requests can't race the
        # active-session check.
        self._lifecycle_lock = asyncio.Lock()

        # Wave 3I: diarization worker reference for pipeline trigger on
        # session stop. Set via :meth:`set_diarization_worker` after
        # construction (the worker depends on the orchestrator's db
        # pool, so wiring is one-way at lifespan setup). ``None`` ⇒
        # diarization disabled / not yet started.
        self._diarization_worker = None  # type: ignore[var-annotated]
        # Phase 5 / Track 5A: per-session OCR worker. The factory
        # builds a fresh worker bound to the current system source on
        # ``start_session``; the worker is stopped (and reference
        # cleared) on ``stop_session``. ``None`` ⇒ OCR disabled or
        # factory not registered. The factory is called only for
        # sessions whose ``system_source is not None`` — i.e. never
        # for ``voice_note`` (mic-only per HELIOS.md §16.12).
        self._ocr_worker_factory = None  # type: ignore[var-annotated]
        self._active_ocr_worker = None  # type: ignore[var-annotated]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_session_id(self) -> int | None:
        """Currently-active session id, or None if none is running."""
        return self._active_session_id

    def set_diarization_worker(self, worker) -> None:
        """Wire the diarization worker reference (Wave 3I).

        Called once at lifespan setup. After ``stop_session`` finishes
        ending the row, the orchestrator enqueues the session id to the
        diarization worker; the diarization worker, in turn, enqueues
        the merge worker once turns have been written. If the worker is
        ``None`` (e.g. diarization disabled or unavailable), the trigger
        is a no-op.
        """
        self._diarization_worker = worker

    def set_ocr_worker_factory(self, factory) -> None:
        """Wire the per-session OCR worker factory (Phase 5 / Track 5A).

        ``factory`` is a sync callable ``(session_id, system_source) →
        OcrWorker`` that returns a fully-constructed (but not yet
        started) worker bound to ``system_source``'s
        ``video_frames()`` iterator. The orchestrator calls
        ``await worker.start()`` after the stream manager comes up and
        ``await worker.stop()`` before tearing the session down. Voice
        notes skip OCR entirely (``system_source is None`` for that
        kind, so the factory is never invoked).

        ``None`` ⇒ OCR disabled or factory not registered — sessions
        run normally without an OCR worker. Mirrors
        :meth:`set_diarization_worker` for wiring symmetry.
        """
        self._ocr_worker_factory = factory

    async def start_session(
        self,
        kind: SessionKind,
        calendar_events: list[CalendarEvent] | None = None,
    ) -> int:
        """Begin a capture session and return the session id.

        Creates the ``capture_sessions`` row, links any provided
        calendar events, builds a chunker, and starts the stream
        manager. Raises ``RuntimeError`` if a session is already
        active (Phase-1 single-session limit).

        Note: the ``capture_sessions.screen_capture_override_until``
        column exists in the schema for Phase-2's manual-screen flow but
        is not wired through this code path yet. When the calendar
        scheduler / manual-screen API land, add a dedicated
        ``update_session_screen_override`` query helper and call it
        here.
        """
        # Take the lifecycle lock for the entire start flow so two
        # concurrent start requests can't both pass the active-check.
        async with self._lifecycle_lock:
            if self._active_session_id is not None:
                raise RuntimeError(
                    f"capture session already active "
                    f"(session_id={self._active_session_id}); "
                    "stop it before starting a new one"
                )

            db = self._db_pool.writer
            started_at = self._clock.time()

            session_id = await queries.create_session(
                db,
                kind=kind,
                started_at=started_at,
            )
            log.info(
                "session_created",
                session_id=session_id,
                kind=kind,
                started_at=started_at,
            )

            # Link any calendar events. Each link is idempotent on the
            # composite (session_id, calendar_event_id) PK; ignore the
            # return value (it's just session_id). At session-start time
            # the session has no ended_at yet, so the overlap is just the
            # event's own window — Phase-2 may want to clip to the actual
            # session bounds once recorded.
            if calendar_events:
                for event in calendar_events:
                    await queries.insert_session_calendar_link(
                        db,
                        session_id=session_id,
                        calendar_event_id=event.id,
                        overlap_start=event.start_ts,
                        overlap_end=event.end_ts,
                    )
                log.info(
                    "session_calendar_links_inserted",
                    session_id=session_id,
                    count=len(calendar_events),
                )

            # Build the per-session chunker. Chunker is not safe for
            # concurrent same-channel writes, but the stream manager
            # spawns exactly one reader per channel, so it's fine.
            chunker = Chunker(
                session_id=session_id,
                storage_root=self._storage_root,
                db=db,
                config=self._config.audio,
                clock=self._clock,
            )

            # Build sources + stream manager. Use a separate queue per
            # channel — the queues are accepted by the source factory
            # for forward-compat with the real (sounddevice / SCK)
            # sources but are unused by the stream manager (which
            # consumes via ``samples()``).
            mic_queue: asyncio.Queue[AudioSample] = asyncio.Queue()
            sys_queue: asyncio.Queue[AudioSample] = asyncio.Queue()
            mic_source = self._factory.make_mic_source(
                queue=mic_queue,
                clock=self._clock,
                start_ts=started_at,
            )
            # Voice notes are mic-only by spec (HELIOS.md §16.12). Skip
            # the system-audio source entirely so we don't initialize
            # ScreenCaptureKit (which probes Screen Recording permission
            # on every stream start, surfacing a macOS prompt the user
            # never opted into for voice notes).
            if kind == "voice_note":
                system_source = None
            else:
                system_source = self._factory.make_system_source(
                    queue=sys_queue,
                    clock=self._clock,
                    start_ts=started_at,
                )

            # Bind the stall callback to THIS session_id via closure so
            # late stalls after a stop can't bleed into the next session.
            on_stall = self._make_stall_callback(session_id)

            stream_manager = StreamManager(
                mic_source=mic_source,
                system_source=system_source,
                chunker=chunker,
                clock=self._clock,
                on_stall=on_stall,
            )

            # Bring the stream up. If startup fails, end the session
            # immediately so no zombie row is left in the DB.
            try:
                await stream_manager.start()
            except Exception as exc:
                log.error(
                    "stream_start_failed",
                    session_id=session_id,
                    error=str(exc),
                )
                await queries.update_session_ended(
                    db,
                    session_id=session_id,
                    ended_at=self._clock.time(),
                    end_reason="stream_start_failed",
                )
                raise

            # Commit the active state only after the stream is up.
            self._active_session_id = session_id
            self._active_chunker = chunker
            self._active_stream_manager = stream_manager

            # Phase 5 / Track 5A — spin up the per-session OCR worker
            # once the system source is live. Skipped when
            # ``system_source is None`` (voice notes) or no factory is
            # registered. Best-effort: any failure here logs but does
            # not abort the session — the stream is already up and
            # capture must continue even if OCR can't.
            if (
                system_source is not None
                and self._ocr_worker_factory is not None
            ):
                try:
                    worker = self._ocr_worker_factory(session_id, system_source)
                    await worker.start()
                    self._active_ocr_worker = worker
                except Exception as exc:  # noqa: BLE001 — best-effort
                    log.warning(
                        "ocr_worker_start_failed",
                        session_id=session_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    self._active_ocr_worker = None

            log.info(
                "session_started",
                session_id=session_id,
                kind=kind,
            )
            return session_id

    async def stop_session(self, session_id: int, reason: str) -> None:
        """End the active session.

        Validates the id matches the active session, stops the stream
        manager (which cancels readers + stops sources), flushes any
        partial chunks, and updates ``capture_sessions.ended_at``.

        Raises ``ValueError`` if no session is active or the id doesn't
        match.
        """
        async with self._lifecycle_lock:
            if self._active_session_id is None:
                raise ValueError("no active capture session to stop")
            if session_id != self._active_session_id:
                raise ValueError(
                    f"session_id mismatch: active={self._active_session_id}, "
                    f"requested={session_id}"
                )

            stream_manager = self._active_stream_manager
            chunker = self._active_chunker
            ocr_worker = self._active_ocr_worker
            assert stream_manager is not None
            assert chunker is not None

            # 0) Stop the per-session OCR worker BEFORE the stream
            # manager so the worker's video-frame iterator doesn't
            # race the source teardown. Best-effort.
            if ocr_worker is not None:
                try:
                    await ocr_worker.stop()
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning(
                        "ocr_worker_stop_failed",
                        session_id=session_id,
                        error=str(exc),
                    )
                self._active_ocr_worker = None

            # 1) Stop the stream manager FIRST so no more samples land
            # in the chunker mid-flush.
            try:
                await stream_manager.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "stream_stop_failed",
                    session_id=session_id,
                    error=str(exc),
                )

            # 2) Flush whatever's left in the chunker as partial chunks.
            try:
                await chunker.flush_partial(reason=reason)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "chunker_flush_failed",
                    session_id=session_id,
                    error=str(exc),
                )

            # 3) Record the session end.
            ended_at = self._clock.time()
            await queries.update_session_ended(
                self._db_pool.writer,
                session_id=session_id,
                ended_at=ended_at,
                end_reason=reason,
            )
            log.info(
                "session_ended",
                session_id=session_id,
                ended_at=ended_at,
                reason=reason,
            )

            # 4) Clear active state.
            self._active_session_id = None
            self._active_chunker = None
            self._active_stream_manager = None

            # 5) Wave 3I — trigger the diarization → merge pipeline.
            # Best-effort: an exception here must not propagate (the
            # session has already ended cleanly). The diarization
            # worker handles "no system audio" / "disabled" cases
            # internally and reports component status accordingly.
            if self._diarization_worker is not None:
                try:
                    await self._diarization_worker.enqueue_session(session_id)
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning(
                        "diarization_enqueue_failed",
                        session_id=session_id,
                        error=str(exc),
                    )

    async def recover(self) -> None:
        """Crash recovery — mark any orphaned sessions as ended.

        Called once on daemon startup after migrations. Any
        ``capture_sessions`` row with ``ended_at IS NULL`` is treated
        as a leftover from an unclean shutdown and finalised with
        ``end_reason='crash_recovery'`` and ``ended_at=clock.time()``.

        We loop because :func:`get_active_session` returns one row at a
        time; in practice there is at most one (the schema's intent),
        but the loop is defensive against past schema bugs.
        """
        db = self._db_pool.writer
        recovered = 0
        while True:
            row = await queries.get_active_session(db)
            if row is None:
                break
            ended_at = self._clock.time()
            await queries.update_session_ended(
                db,
                session_id=row.id,
                ended_at=ended_at,
                end_reason="crash_recovery",
            )
            recovered += 1
            log.warning(
                "crash_recovery_session_ended",
                session_id=row.id,
                kind=row.kind,
                started_at=row.started_at,
                ended_at=ended_at,
            )
        if recovered:
            log.info("crash_recovery_complete", sessions_recovered=recovered)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_stall_callback(self, session_id: int):
        """Build a stall callback bound to a specific session id.

        Closing over the session_id (instead of reading
        ``self._active_session_id``) means a stall fired after the
        session has been stopped will still log/record against the
        right session, never the next one.
        """

        async def _on_stall(
            channel: str,
            gap_start_ts: float,
            gap_end_ts: float,
            reason: str = "watchdog_stall",
        ) -> None:
            try:
                samples = max(
                    0,
                    int(
                        (gap_end_ts - gap_start_ts)
                        * self._config.audio.sample_rate
                    ),
                )
                await queries.insert_unavailable_chunk(
                    self._db_pool.writer,
                    session_id=session_id,
                    channel=channel,
                    start_ts=gap_start_ts,
                    end_ts=gap_end_ts,
                    samples=samples,
                    reason=reason,
                )
                log.warning(
                    "stall_recorded",
                    session_id=session_id,
                    channel=channel,
                    reason=reason,
                    gap_seconds=round(gap_end_ts - gap_start_ts, 3),
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "stall_record_failed",
                    session_id=session_id,
                    channel=channel,
                    reason=reason,
                    error=str(exc),
                )

        return _on_stall


__all__ = ["CaptureOrchestrator", "SessionKind"]
