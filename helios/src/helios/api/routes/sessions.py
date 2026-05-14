"""``/v1/sessions/*`` endpoints.

Per HELIOS.md §7.3:

* ``GET /sessions``                     — filterable list (kind, time, status).
* ``GET /sessions/{id}``               — detail.
* ``GET /sessions/{id}/transcript``    — placeholder until Phase 3.
* ``POST /sessions/{id}/re-transcribe`` — queues re-transcription (Phase 3).
* ``POST /sessions/{id}/re-diarize``    — queues re-diarization (Phase 3).
* ``DELETE /sessions/{id}``            — deletes session + cascade.

The actual queue is wired in Phase 3 (Track 3D); the action endpoints
return 202 ``{"status": "queued"}`` so clients have a stable contract.
"""

from __future__ import annotations

import json
from pathlib import Path as _FsPath
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status

from helios.api.schemas import (
    SessionActionResponse,
    SessionDeleteResponse,
    SessionResponse,
    SessionsListResponse,
    TranscriptCoverage,
    TranscriptCoverageRange,
    TranscriptResponse,
    TranscriptSegmentResponse,
)
from helios.capture.coverage import compute_session_coverage
from helios.db import queries
from helios.log import get_logger
from helios.workers.cleanup import trash_session_audio

router = APIRouter()
log = get_logger("api.sessions")


def _err(code: int, error: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"error": error, "detail": detail, "code": code},
    )


def _row_to_response(row) -> SessionResponse:
    duration = (
        row.ended_at - row.started_at if row.ended_at is not None else None
    )
    return SessionResponse(
        id=row.id,
        kind=row.kind,
        started_at=row.started_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        duration_seconds=duration,
        diarization_status=row.diarization_status,
        screen_capture_override_until=row.screen_capture_override_until,
    )


@router.get("/sessions", response_model=SessionsListResponse)
async def list_sessions(
    request: Request,
    kind: str | None = Query(default=None),
    start_ts_gte: float | None = Query(default=None),
    start_ts_lte: float | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> SessionsListResponse:
    db = request.app.state.db_pool.writer
    rows = await queries.list_sessions(
        db,
        kind=kind,
        start_ts_gte=start_ts_gte,
        start_ts_lte=start_ts_lte,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    total = await queries.count_sessions(
        db,
        kind=kind,
        start_ts_gte=start_ts_gte,
        start_ts_lte=start_ts_lte,
        status=status_filter,
    )
    return SessionsListResponse(
        sessions=[_row_to_response(r) for r in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    request: Request,
    session_id: int = Path(..., ge=1),
) -> SessionResponse:
    db = request.app.state.db_pool.writer
    row = await queries.get_session_by_id(db, session_id)
    if row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )
    return _row_to_response(row)


def _decode_words(words: str | None) -> list | None:
    """Parse the JSON-encoded ``words`` column into a list of dicts.

    Returns ``None`` when the column is empty or contains invalid JSON
    so a malformed row never raises a 500.
    """
    if not words:
        return None
    try:
        decoded = json.loads(words)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, list):
        return None
    return decoded


@router.get(
    "/sessions/{session_id}/transcript",
    response_model=TranscriptResponse,
)
async def get_session_transcript(
    request: Request,
    session_id: int = Path(..., ge=1),
    include_words: bool = Query(default=False),
) -> TranscriptResponse:
    """Per HELIOS.md §9.5 — segments + coverage object for one session."""
    db = request.app.state.db_pool.writer
    row = await queries.get_session_by_id(db, session_id)
    if row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )

    config = request.app.state.config
    segments = await queries.get_segments_for_session(db, session_id)
    coverage = await compute_session_coverage(
        db, session_id, sample_rate=config.audio.sample_rate
    )

    return TranscriptResponse(
        session_id=session_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        segments=[
            TranscriptSegmentResponse(
                start=s.start_ts,
                end=s.end_ts,
                speaker=s.speaker,
                text=s.text,
                words=_decode_words(s.words) if include_words else None,
            )
            for s in segments
        ],
        coverage=TranscriptCoverage(
            captured_seconds=coverage.captured_seconds,
            unavailable_ranges=[
                TranscriptCoverageRange(
                    start=r.start_ts,
                    end=r.end_ts,
                    reason=r.reason,
                )
                for r in coverage.unavailable_ranges
            ],
            transcription_pending_seconds=coverage.transcription_pending_seconds,
        ),
        diarization_status=row.diarization_status,
    )


@router.post(
    "/sessions/{session_id}/re-transcribe",
    response_model=SessionActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def re_transcribe(
    request: Request,
    session_id: int = Path(..., ge=1),
) -> SessionActionResponse:
    """Phase-2 stub — Phase 3 (Track 3D) wires the real queue."""
    db = request.app.state.db_pool.writer
    row = await queries.get_session_by_id(db, session_id)
    if row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )
    log.info("re_transcribe_queued", session_id=session_id)
    return SessionActionResponse(status="queued", session_id=session_id)


@router.post(
    "/sessions/{session_id}/re-diarize",
    response_model=SessionActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def re_diarize(
    request: Request,
    session_id: int = Path(..., ge=1),
) -> SessionActionResponse:
    """Phase-2 stub — Phase 3 wires the real diarization queue."""
    db = request.app.state.db_pool.writer
    row = await queries.get_session_by_id(db, session_id)
    if row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )
    log.info("re_diarize_queued", session_id=session_id)
    return SessionActionResponse(status="queued", session_id=session_id)


@router.delete(
    "/sessions/{session_id}",
    response_model=SessionDeleteResponse,
)
async def delete_session(
    request: Request,
    session_id: int = Path(..., ge=1),
) -> SessionDeleteResponse:
    """Delete a single capture session — see HELIOS.md §7.3 + §17.

    Flow:

    1. 404 if the session does not exist.
    2. If the session is the orchestrator's active session, stop it
       (orchestrator + scheduler), then proceed.
    3. Move every chunk's WAV to ``<storage.root>/trash/`` (preserves
       the 24-hour grace window the nightly cleanup also relies on).
    4. ``DELETE FROM capture_sessions WHERE id = ?`` — foreign keys
       cascade to ``audio_chunks`` / ``transcript_segments`` /
       ``diarization_turns`` / ``ocr_frames`` / ``voice_notes``
       (see migrations/001_initial.sql).
    """
    db = request.app.state.db_pool.writer
    row = await queries.get_session_by_id(db, session_id)
    if row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )

    orch = request.app.state.orchestrator
    scheduler = getattr(request.app.state, "scheduler", None)
    was_active = orch.active_session_id == session_id

    if was_active:
        # Stop the active capture before we yank its rows out from
        # under it. Mirror the voice-note stop pattern: orchestrator
        # first, then scheduler bookkeeping, both wrapped in try/except
        # so a partial failure can't leave the row resident.
        try:
            await orch.stop_session(session_id, reason="user_delete")
        except ValueError as exc:
            # Active session vanished between our active_session_id
            # read and the stop — fine, fall through to deletion.
            log.info(
                "delete_session_orchestrator_already_inactive",
                session_id=session_id,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — never block delete
            log.warning(
                "delete_session_orchestrator_stop_failed",
                session_id=session_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        if scheduler is not None:
            try:
                await scheduler.notify_session_stopped(session_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "delete_session_scheduler_notify_failed",
                    session_id=session_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    # Resolve storage root for the trash directory. Stays consistent
    # with the cleanup worker's choice of ``<root>/trash/``.
    storage_root_str = request.app.state.config.storage.root
    storage_root = _FsPath(storage_root_str).expanduser()

    chunks_trashed = await trash_session_audio(
        db=db,
        session_id=session_id,
        storage_root=storage_root,
    )

    # Hard-delete the capture session — cascade handles chunks /
    # segments / diarization / OCR / voice notes per the migration.
    deleted = await queries.delete_session(db, session_id)
    if not deleted:
        # Race: the row vanished between the get and the delete. Treat
        # as 404 so callers can retry idempotently.
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )

    log.info(
        "session_deleted",
        session_id=session_id,
        was_active=was_active,
        chunks_trashed=chunks_trashed,
    )

    return SessionDeleteResponse(
        session_id=session_id,
        chunks_trashed=chunks_trashed,
        was_active=was_active,
    )
