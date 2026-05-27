"""``/v1/sessions/*`` endpoints.

Per HELIOS.md §7.3:

* ``GET /sessions``                     — filterable list (kind, time, status, date).
* ``GET /sessions/{id}``               — detail.
* ``GET /sessions/{id}/transcript``    — segments + coverage.
* ``POST /sessions/{id}/re-transcribe`` — requeue all chunks for transcription.
* ``POST /sessions/{id}/re-diarize``    — clear turns, re-enqueue diarization.
* ``DELETE /sessions/{id}``            — deletes session + cascade.

Wave 6D (Track 6D) made ``re-transcribe`` / ``re-diarize`` real:
they manipulate DB state directly and enqueue the diarization
worker. The list endpoint additionally accepts ``date=YYYY-MM-DD``
to filter to a single local-tz day.
"""

from __future__ import annotations

import json
from datetime import date as date_cls, datetime, time as time_cls
from pathlib import Path as _FsPath
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import FileResponse

from helios.api.schemas import (
    AudioChunkResponse,
    ReDiarizeResponse,
    ReTranscribeResponse,
    SessionActionResponse,
    SessionAudioChunksResponse,
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


def _parse_date_to_local_day_range(date_str: str) -> tuple[float, float]:
    """Convert an ISO ``YYYY-MM-DD`` string to a (gte, lte) epoch window.

    The window spans the entire local-tz day [00:00:00.000,
    23:59:59.999999]. Raises ``ValueError`` for invalid input — the
    caller maps that to a 422.
    """
    parsed = date_cls.fromisoformat(date_str)
    start_dt = datetime.combine(parsed, time_cls.min).astimezone()
    end_dt = datetime.combine(parsed, time_cls.max).astimezone()
    return (start_dt.timestamp(), end_dt.timestamp())


@router.get("/sessions", response_model=SessionsListResponse)
async def list_sessions(
    request: Request,
    kind: str | None = Query(default=None),
    start_ts_gte: float | None = Query(default=None),
    start_ts_lte: float | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    date: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> SessionsListResponse:
    """List capture sessions, newest first.

    Filters (all optional):

    * ``kind``         — exact match on ``capture_sessions.kind``.
    * ``start_ts_gte`` / ``start_ts_lte`` — epoch second bounds.
    * ``status``       — ``active`` (ended_at IS NULL),
      ``ended``/``completed``/``captured`` (ended_at IS NOT NULL), or
      ``failed`` (end_reason LIKE failure marker).
    * ``date``         — ISO-8601 ``YYYY-MM-DD`` filter to the local-tz day.
      Mutually layered with start_ts_gte/lte (whichever is tighter wins
      via AND).
    """
    if date is not None:
        try:
            day_gte, day_lte = _parse_date_to_local_day_range(date)
        except ValueError as exc:
            raise _err(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_date",
                f"date must be ISO YYYY-MM-DD: {exc}",
            )
        # Layer onto any caller-supplied bounds by taking the tighter
        # window (max of lower bounds, min of upper bounds).
        if start_ts_gte is None or day_gte > start_ts_gte:
            start_ts_gte = day_gte
        if start_ts_lte is None or day_lte < start_ts_lte:
            start_ts_lte = day_lte

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


@router.get(
    "/sessions/{session_id}/audio-chunks",
    response_model=SessionAudioChunksResponse,
)
async def get_session_audio_chunks_endpoint(
    request: Request,
    session_id: int = Path(..., ge=1),
) -> SessionAudioChunksResponse:
    """Audio-chunk inventory for one session, ordered by start_ts.

    Powers the dashboard's Audio tab. The actual WAV bytes are served
    by ``GET /v1/sessions/{id}/audio/{chunk_id}`` so the client can use
    a normal ``<audio>`` element. ``has_audio_file`` is False when the
    chunk has been archived by the retention worker (path = NULL).
    """
    db = request.app.state.db_pool.writer
    session_row = await queries.get_session_by_id(db, session_id)
    if session_row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )
    chunks = await queries.get_session_audio_chunks(db, session_id)
    return SessionAudioChunksResponse(
        session_id=session_id,
        chunks=[
            AudioChunkResponse(
                id=c.id,
                session_id=c.session_id,
                channel=c.channel,
                start_ts=c.start_ts,
                end_ts=c.end_ts,
                samples=c.samples,
                status=c.status,
                partial=bool(getattr(c, "partial", False)),
                duration_seconds=max(0.0, c.end_ts - c.start_ts),
                has_audio_file=bool(c.path),
            )
            for c in chunks
        ],
    )


@router.get("/sessions/{session_id}/audio/{chunk_id}")
async def get_session_audio_file(
    request: Request,
    session_id: int = Path(..., ge=1),
    chunk_id: int = Path(..., ge=1),
) -> FileResponse:
    """Stream the WAV bytes for one audio chunk.

    Path-safety: chunks are referenced by their DB id, which is verified
    to belong to ``session_id``. The on-disk path stored in the row is
    written by the capture worker (never user-controlled), so we don't
    need additional traversal guards beyond confirming it exists.
    """
    db = request.app.state.db_pool.writer
    chunk = await queries.get_chunk_by_id(db, chunk_id)
    if chunk is None or chunk.session_id != session_id:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "chunk_not_found",
            f"chunk {chunk_id} not found in session {session_id}",
        )
    if not chunk.path:
        raise _err(
            status.HTTP_410_GONE,
            "chunk_archived",
            f"chunk {chunk_id} has been archived (no audio file on disk)",
        )
    fs_path = _FsPath(chunk.path)
    if not fs_path.exists():
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "chunk_file_missing",
            f"chunk {chunk_id} file no longer exists on disk",
        )
    return FileResponse(
        path=str(fs_path),
        media_type="audio/wav",
        filename=f"session-{session_id}-chunk-{chunk_id}.wav",
    )


@router.post(
    "/sessions/{session_id}/re-transcribe",
    response_model=ReTranscribeResponse,
)
async def re_transcribe(
    request: Request,
    session_id: int = Path(..., ge=1),
) -> ReTranscribeResponse:
    """Re-enqueue every chunk in ``session_id`` for transcription.

    Clears existing ``transcript_segments`` rows tied to the session
    (so the worker doesn't end up with double-extracted text), then
    flips each chunk's ``status`` back to ``recorded`` and resets
    ``transcribed_at`` / ``transcription_attempts``. The transcription
    worker's next poll picks them up.
    """
    db = request.app.state.db_pool.writer
    row = await queries.get_session_by_id(db, session_id)
    if row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )
    # Clear segments BEFORE flipping chunk status so a racing poll
    # can't accidentally append new segments alongside stale ones.
    await queries.clear_transcript_segments_for_session(db, session_id)
    requeued = await queries.requeue_chunks_for_session(db, session_id)
    log.info(
        "re_transcribe_requeued",
        session_id=session_id,
        chunks_requeued=requeued,
    )
    return ReTranscribeResponse(
        session_id=session_id, chunks_requeued=requeued
    )


@router.post(
    "/sessions/{session_id}/re-diarize",
    response_model=ReDiarizeResponse,
)
async def re_diarize(
    request: Request,
    session_id: int = Path(..., ge=1),
) -> ReDiarizeResponse:
    """Re-enqueue diarization for ``session_id``.

    Clears prior ``diarization_turns`` rows + resets the session's
    ``diarization_status`` to ``pending``, then attempts to enqueue
    the session id onto the diarization worker. The worker handles
    "no system audio" / "disabled" cases internally; if it's None
    (disabled / not wired), the response carries
    ``jobs_requeued=0`` so the dashboard can show a hint.
    """
    db = request.app.state.db_pool.writer
    row = await queries.get_session_by_id(db, session_id)
    if row is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {session_id} not found",
        )
    await queries.clear_diarization_turns_for_session(db, session_id)
    await queries.update_session_diarization_status(
        db, session_id, status="pending"
    )
    diar_worker = getattr(request.app.state, "diarization_worker", None)
    jobs_requeued = 0
    if diar_worker is not None:
        try:
            await diar_worker.enqueue_session(session_id)
            jobs_requeued = 1
        except Exception as exc:  # noqa: BLE001 — best effort
            log.warning(
                "re_diarize_enqueue_failed",
                session_id=session_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
    log.info(
        "re_diarize_requeued",
        session_id=session_id,
        jobs_requeued=jobs_requeued,
    )
    return ReDiarizeResponse(
        session_id=session_id, jobs_requeued=jobs_requeued
    )


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
