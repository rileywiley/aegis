"""Helios HTTP API — FastAPI app factory.

Wave 3 (Track 2E) extends the Phase-1 minimal surface with the full
endpoint set from HELIOS.md §7.3 plus a bearer-token middleware applied
to every router except :mod:`helios.api.routes.health`.

Error envelope handling
-----------------------
HELIOS.md §7.2 specifies a flat ``{"error", "detail", "code"}`` shape on
every non-2xx response. FastAPI's default ``HTTPException`` handler
nests payloads under ``{"detail": <whatever>}``. We register a custom
handler that:

* If ``detail`` is already a dict containing ``error`` + ``code``, it is
  returned verbatim (route handlers raise this shape via the local
  ``_err`` helpers).
* Otherwise the handler synthesises the envelope with ``error`` derived
  from the HTTP status name (e.g. 404 → ``"not_found"``).

A separate handler flattens Pydantic validation errors (422) into a
``validation_error`` envelope so clients can match a single
discriminator.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from helios.api.auth import require_token
from helios.api.routes import (
    audio,
    capture,
    diagnostics,
    health,
    ocr,
    sessions,
    voice_note,
)
from helios.api.routes import status as status_routes
from helios.capture.orchestrator import CaptureOrchestrator
from helios.clock import RealClock
from helios.components import ComponentStatusReporter
from helios.config import load_config
from helios.db.connection import DatabasePool
from helios.db.migrations import get_migrations_dir, run_migrations
from helios.log import get_logger
from helios.scheduler.calendar import CalendarClient
from helios.scheduler.scheduler import Scheduler
from helios.sources import make_source_factory
from helios.state import DaemonStateMachine
from helios.workers.cleanup import CleanupWorker
from helios.workers.diarization import DiarizationWorker
from helios.workers.merge import MergeWorker
from helios.workers.permissions import PermissionChecker
from helios.workers.transcription import TranscriptionWorker

log = get_logger("api")


def _aegis_base_url(full_endpoint_url: str) -> str:
    """Strip ``/api/...`` suffix from the configured calendar URL.

    ``CalendarClient`` takes only the host base; the full path is added
    internally. Defensive against trailing slashes / extra paths.

    Raises ``ValueError`` if the URL has no host component — otherwise
    misconfiguration would surface as cryptic httpx connect errors much
    later (e.g. ``calendar_poll_failed connect to http:///...``).
    """
    parsed = urlparse(full_endpoint_url)
    if not parsed.scheme:
        # Assume http if missing.
        full_endpoint_url = "http://" + full_endpoint_url
        parsed = urlparse(full_endpoint_url)
    if not parsed.netloc:
        raise ValueError(
            f"calendar_source_url must include scheme + host, got: "
            f"{full_endpoint_url!r}"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    db_path = Path(config.storage.root).expanduser() / "index.db"
    pool = DatabasePool(db_path)
    orchestrator: CaptureOrchestrator | None = None
    scheduler: Scheduler | None = None
    permission_checker: PermissionChecker | None = None
    transcription_worker: TranscriptionWorker | None = None
    diarization_worker: DiarizationWorker | None = None
    merge_worker: MergeWorker | None = None
    cleanup_worker: CleanupWorker | None = None
    aegis_http: httpx.AsyncClient | None = None
    pool_opened = False
    try:
        await pool.open()
        pool_opened = True
        version = await run_migrations(pool.writer, get_migrations_dir())
        log.info("migrations_complete", version=version, path=str(db_path))

        # Build the orchestrator and run crash recovery before serving
        # traffic. If recover() raises, the finally clause still closes
        # the pool (it was opened above).
        clock = RealClock()
        source_factory = make_source_factory(config)
        orchestrator = CaptureOrchestrator(pool, config, clock, source_factory)
        await orchestrator.recover()

        # Build the daemon state machine.
        state_machine = DaemonStateMachine()

        # Calendar source: real HTTP client to Aegis OR fixture-driven
        # replay source (under HELIOS_REPLAY=1).
        if os.environ.get("HELIOS_REPLAY") == "1":
            calendar_source = source_factory.make_calendar_source(clock)
        else:
            aegis_http = httpx.AsyncClient()
            calendar_source = CalendarClient(
                base_url=_aegis_base_url(config.scheduler.calendar_source_url),
                http=aegis_http,
                timeout_seconds=config.scheduler.aegis_connect_timeout_seconds,
            )

        scheduler = Scheduler(
            clock=clock,
            calendar_source=calendar_source,
            orchestrator=orchestrator,
            state_machine=state_machine,
            config=config,
        )
        await scheduler.start()

        permission_checker = PermissionChecker(
            clock=clock,
            db=pool.writer,
            state_machine=state_machine,
            check_interval_seconds=config.scheduler.permission_check_minutes * 60,
        )
        await permission_checker.start()

        # Component status reporter — Wave 1A. Persists per-subsystem
        # health to component_status and reflects errors into the daemon
        # state machine. The transcription worker (Wave 2E / Track 3B)
        # uses it to surface model load/runtime status.
        component_reporter = ComponentStatusReporter(
            db=pool.writer, state_machine=state_machine
        )

        # Transcription worker — Wave 2E / Track 3B. Loads WhisperX in a
        # background thread so the daemon serves /v1/health immediately,
        # then drains audio_chunks → transcript_segments. Voice notes
        # call its ``transcribe_synchronously`` method directly.
        transcription_worker = TranscriptionWorker(
            db=pool.writer,
            config=config,
            clock=clock,
            reporter=component_reporter,
        )
        await transcription_worker.start()

        # Wave 3I — diarization + merge workers complete the pipeline:
        # orchestrator.stop_session → diarization.enqueue_session →
        # diarization writes turns → merge.enqueue_session → merge
        # assigns transcript_segments.speaker for system-channel rows.
        # The diarization worker is a no-op (reports unavailable) when
        # disabled or missing the HF token; the merge worker is then
        # never triggered, segments stay speaker=NULL.
        diarization_worker = DiarizationWorker(
            db=pool.writer,
            config=config,
            clock=clock,
            reporter=component_reporter,
        )
        merge_worker = MergeWorker(db=pool.writer)
        diarization_worker.set_merge_worker(merge_worker)
        orchestrator.set_diarization_worker(diarization_worker)
        await diarization_worker.start()
        await merge_worker.start()

        # Phase 5 / Track 5C — daily retention sweep. Trashes WAVs
        # older than ``retention.raw_audio_days`` (skips untranscribed
        # so the user never loses unprocessed content), then purges
        # the trash dir after ``trash_hold_hours``. Runs at
        # ``retention.cleanup_hour_local`` each day.
        cleanup_worker = CleanupWorker(
            db=pool.writer,
            config=config,
            clock=clock,
        )
        await cleanup_worker.start()

        app.state.db_pool = pool
        app.state.config = config
        app.state.clock = clock
        app.state.orchestrator = orchestrator
        app.state.scheduler = scheduler
        app.state.state_machine = state_machine
        app.state.permission_checker = permission_checker
        app.state.component_reporter = component_reporter
        app.state.transcription_worker = transcription_worker
        app.state.diarization_worker = diarization_worker
        app.state.merge_worker = merge_worker
        app.state.cleanup_worker = cleanup_worker
        # Voice-note tracking lives on app.state — see
        # helios.api.routes.voice_note for usage.
        app.state.voice_note_active = None
        yield
    finally:
        # Stop background workers FIRST so they don't fight with the
        # session shutdown below.
        if scheduler is not None:
            try:
                await scheduler.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("scheduler_stop_failed", error=str(exc))
        if permission_checker is not None:
            try:
                await permission_checker.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("permission_checker_stop_failed", error=str(exc))
        if transcription_worker is not None:
            try:
                await transcription_worker.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("transcription_worker_stop_failed", error=str(exc))
        # Wave 3I: stop merge BEFORE diarization so a late
        # diarization completion doesn't enqueue work onto a
        # cancelled merge worker.
        if merge_worker is not None:
            try:
                await merge_worker.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("merge_worker_stop_failed", error=str(exc))
        if diarization_worker is not None:
            try:
                await diarization_worker.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("diarization_worker_stop_failed", error=str(exc))
        if cleanup_worker is not None:
            try:
                await cleanup_worker.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("cleanup_worker_stop_failed", error=str(exc))
        # If a session is still active at shutdown, stop it cleanly so
        # the partial chunk lands on disk. orchestrator may be None if
        # an earlier setup step failed.
        if orchestrator is not None:
            active = orchestrator.active_session_id
            if active is not None:
                try:
                    await orchestrator.stop_session(
                        active, reason="daemon_shutdown"
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("shutdown_stop_failed", error=str(exc))
        if aegis_http is not None:
            try:
                await aegis_http.aclose()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("aegis_http_close_failed", error=str(exc))
        if pool_opened:
            await pool.close()


def _http_status_error_code(code: int) -> str:
    """Map a numeric HTTP status to a stable string error code."""
    try:
        name = HTTPStatus(code).name.lower()
    except ValueError:
        name = "internal_error"
    return name


async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Flatten ``HTTPException`` payloads to the §7.2 envelope."""
    detail: Any = exc.detail
    if isinstance(detail, dict) and "error" in detail and "code" in detail:
        # Route handlers raised the canonical envelope already.
        body = {
            "error": detail.get("error"),
            "detail": detail.get("detail", ""),
            "code": int(detail.get("code", exc.status_code)),
        }
    else:
        body = {
            "error": _http_status_error_code(exc.status_code),
            "detail": str(detail) if detail is not None else "",
            "code": exc.status_code,
        }
    return JSONResponse(status_code=exc.status_code, content=body)


async def _validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Flatten Pydantic 422 errors into a ``validation_error`` envelope.

    The full Pydantic error list is preserved under ``detail`` as a
    JSON-encoded string for clients that want to introspect it.
    """
    import json

    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "detail": json.dumps(exc.errors(), default=str),
            "code": 422,
        },
    )


def create_app() -> FastAPI:
    """Create the Helios FastAPI application.

    HELIOS.md §7.1: only ``/v1/health`` is unauthenticated. We disable
    the OpenAPI schema dump (``openapi_url=None``) so the endpoint
    surface isn't leaked to anyone hitting loopback. Docs UIs are also
    off (``docs_url`` / ``redoc_url``).
    """
    app = FastAPI(
        title="Helios",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # Custom error envelope (HELIOS.md §7.2).
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)

    # Health is the sole unauthenticated endpoint.
    app.include_router(health.router, prefix="/v1")

    # Every other route requires the bearer token.
    authed = [Depends(require_token)]
    app.include_router(capture.router, prefix="/v1", dependencies=authed)
    app.include_router(status_routes.router, prefix="/v1", dependencies=authed)
    app.include_router(sessions.router, prefix="/v1", dependencies=authed)
    app.include_router(audio.router, prefix="/v1", dependencies=authed)
    app.include_router(ocr.router, prefix="/v1", dependencies=authed)
    app.include_router(diagnostics.router, prefix="/v1", dependencies=authed)
    app.include_router(voice_note.router, prefix="/v1", dependencies=authed)

    return app
