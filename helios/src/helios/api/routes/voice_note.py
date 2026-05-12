"""``/v1/voice-note/*`` endpoints.

Per HELIOS.md §7.3 voice notes are user-triggered short captures (≤5 min)
with synchronous transcription. Phase 3 (Track 3B) wires transcription
through ``TranscriptionWorker.transcribe_synchronously`` — ``stop`` now
returns the joined transcript text and per-segment data inline.

Active-voice-note state is tracked on ``app.state.voice_note_active`` —
a plain dict that holds the in-flight session id and metadata. Updates
flow through this module only (single owner).

# TODO Phase 3: persist active voice-note marker to a ``voice_notes_active``
# row so a daemon restart doesn't lose the parent-session linkage.
# Currently a restart drops the marker and the next start may double-create
# while a stop may 404 even if the parent session is mid-recording.

Mic permission gate
-------------------
We refuse to start a voice note when the most recent permission check
shows ``mic_granted=False``. Rationale: a voice note that can't capture
mic audio is useless, and rejecting up-front gives the menu bar a
cleaner UX than letting the chunker write empty no_audio frames.

Transcription availability gate
-------------------------------
The §7.3 spec calls for ``503 component_unavailable`` when transcription
is down. Wave 2E reads the in-memory ``ComponentStatusReporter`` snapshot
(``app.state.component_reporter``). ``error`` and most ``unavailable``
reasons (whisperx_not_installed, model_load_timeout, etc.) reject the
start with 503. The transient ``unavailable(reason=model_loading)``
state is allowed through — by the time the user finishes recording, the
model has loaded and ``transcribe_synchronously`` simply waits on
``_model_ready``.
"""

from __future__ import annotations

import math
import wave
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from helios.api.schemas import (
    ActiveVoiceNote,
    ActiveVoiceNoteResponse,
    TranscriptCoverage,
    TranscriptResponse,
    TranscriptSegmentResponse,
    VoiceNoteCancelResponse,
    VoiceNoteStartRequest,
    VoiceNoteStartResponse,
    VoiceNoteStopResponse,
)
from helios.db import queries
from helios.log import get_logger

router = APIRouter()
log = get_logger("api.voice_note")

# Cap-warning fires within this many seconds of max_duration_seconds.
_APPROACHING_CAP_SECONDS = 30.0

# Track 4G: window of audio sampled for the live RMS reading. 250ms at
# 16kHz int16 mono = 4000 samples = 8000 bytes — cheap to read off disk
# even on a slow APFS volume.
_RMS_WINDOW_SECONDS = 0.25
_RMS_INT16_DENOM = 32768.0


def _compute_recent_rms(path: str | None) -> float:
    """Return the RMS of the trailing 250ms of a mono int16 WAV.

    Caller passes the path of the most recently recorded audio chunk on
    the active voice-note's session. Returns ``0.0`` on any error
    (missing file, malformed header, empty body) — the indicator just
    shows a quiet meter rather than crashing.

    Notes
    -----
    * No audio samples are logged. Only the scalar RMS is exposed.
    * Reads at most ``_RMS_WINDOW_SECONDS * sample_rate`` frames so the
      cost stays bounded even when chunks grow to 30s.
    """
    if not path:
        return 0.0
    try:
        # ``numpy`` is a hard dep already (used by the worker) — local
        # import keeps API import cost low.
        import numpy as np

        with wave.open(path, "rb") as wf:
            nframes = wf.getnframes()
            if nframes <= 0:
                return 0.0
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate() or 16000
            n_channels = wf.getnchannels() or 1
            window_frames = int(_RMS_WINDOW_SECONDS * framerate)
            if window_frames <= 0:
                return 0.0
            read_frames = min(window_frames, nframes)
            start_frame = nframes - read_frames
            wf.setpos(start_frame)
            raw = wf.readframes(read_frames)
        if not raw:
            return 0.0
        if sampwidth != 2:
            # Unexpected width — Helios always writes int16. Bail out
            # rather than misinterpret the bytes.
            return 0.0
        samples = np.frombuffer(raw, dtype=np.int16)
        if n_channels > 1:
            # Mix-down by averaging interleaved channels.
            samples = samples.reshape(-1, n_channels).mean(axis=1)
        if samples.size == 0:
            return 0.0
        # Cast to float64 BEFORE squaring so the int16 squares don't
        # overflow. Normalise by full-scale int16 to land in [0, 1].
        floats = samples.astype("float64") / _RMS_INT16_DENOM
        rms = float(np.sqrt(np.mean(floats * floats)))
        if math.isnan(rms) or math.isinf(rms):
            return 0.0
        # Clamp defensively — well-formed int16 data can't exceed 1.0
        # post-normalisation but a malformed file might.
        return max(0.0, min(1.0, rms))
    except (wave.Error, OSError, ValueError):
        return 0.0
    except Exception:  # noqa: BLE001 — defensive, RMS is best-effort
        return 0.0


def _err(code: int, error: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"error": error, "detail": detail, "code": code},
    )


def _now(request: Request) -> float:
    """Current wall-clock time from the app-state Clock.

    Centralised so route handlers don't reach into ``orch._clock`` /
    fall back to a meaningless ``0.0`` sentinel.
    """
    return request.app.state.clock.time()


def _active(request: Request) -> dict[str, Any] | None:
    """Return the active voice-note state, auto-clearing if stale.

    ``voice_note_active`` is set by ``/voice-note/start`` and cleared by
    ``/voice-note/stop``. The scheduler's cap-timer force-stop path
    (``Scheduler._on_voice_note_force_stop``) bypasses the stop
    endpoint, so the orchestrator's active session can disappear while
    this dict still says a voice note is in flight. The indicator polls
    ``/v1/voice-note/active`` every 250ms and only dismisses when
    ``active=null``; without this cross-check it never sees null after
    a force-stop and the floating window stays on screen indefinitely.

    Excerpt mode stores the parent session's id as ``session_id``, so
    a single ``orch.active_session_id == state["session_id"]`` check
    covers both standalone and excerpt voice notes.
    """
    state = getattr(request.app.state, "voice_note_active", None)
    if state is None:
        return None
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return state
    if orch.active_session_id != state["session_id"]:
        request.app.state.voice_note_active = None
        return None
    return state


def _set_active(request: Request, value: dict[str, Any] | None) -> None:
    request.app.state.voice_note_active = value


@router.post(
    "/voice-note/start",
    response_model=VoiceNoteStartResponse,
)
async def voice_note_start(
    req: VoiceNoteStartRequest, request: Request
) -> VoiceNoteStartResponse:
    """Begin a voice note. Errors per HELIOS.md §7.3:

    * 403 ``permission_denied`` when mic isn't granted.
    * 409 ``voice_note_already_active`` when one is already running.
    * 503 ``component_unavailable`` when transcription is down (Phase 3).
    """
    db = request.app.state.db_pool.writer
    config = request.app.state.config
    orch = request.app.state.orchestrator
    scheduler = request.app.state.scheduler

    # 1) Mic permission gate.
    perm = await queries.get_last_permission_check(db)
    if perm is not None and not perm.mic_granted:
        raise _err(
            status.HTTP_403_FORBIDDEN,
            "permission_denied",
            "microphone permission not granted",
        )

    # 2) Already-active gate.
    if _active(request) is not None:
        raise _err(
            status.HTTP_409_CONFLICT,
            "voice_note_already_active",
            "a voice note is already recording",
        )

    # 3) Transcription gate — Wave 2E. Reject 503 when the worker reports
    # a hard failure or missing dependency. The transient ``model_loading``
    # state is treated as available because the synchronous-stop call
    # blocks on ``_model_ready`` and the model finishes loading well
    # within the user's recording window.
    reporter = getattr(request.app.state, "component_reporter", None)
    if reporter is not None:
        rec = reporter.get("transcription")
        if rec is not None:
            if rec.status == "error" or (
                rec.status == "unavailable" and rec.reason != "model_loading"
            ):
                raise _err(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "component_unavailable",
                    rec.detail or rec.reason or "transcription not available",
                )

    # 4) Excerpt detection: if a calendar/continuous session is currently
    # active, this voice note becomes an excerpt.
    parent_session_id: int | None = None
    is_excerpt = False
    parent_active = orch.active_session_id
    if parent_active is not None:
        parent_kind = (
            scheduler.get_session_kind(parent_active) if scheduler else None
        )
        if parent_kind in ("calendar", "continuous"):
            is_excerpt = True
            parent_session_id = parent_active

    if is_excerpt:
        # Excerpt mode: reuse parent session id, no new session row.
        # Phase 3 will record voice_notes.excerpt_of_session_id +
        # excerpt_start_ts/end_ts. For Phase 2 we only return the wire
        # contract.
        session_id = parent_session_id
        # Mint a synthetic voice-note id from the parent session id +
        # request count so the menu bar can correlate. Until the
        # voice_notes table is wired (Phase 3), reuse the session id.
        voice_note_id = session_id
        started_at = _now(request)
    else:
        # Standalone voice note — a fresh session_id from the orchestrator.
        try:
            session_id = await orch.start_session(kind="voice_note")
        except RuntimeError as exc:
            # Another session became active between the check above and
            # here — surface as already-active.
            raise _err(
                status.HTTP_409_CONFLICT,
                "voice_note_already_active",
                str(exc),
            )
        # Notify the scheduler so cap timers + kind-tracking apply.
        try:
            await scheduler.notify_session_started(session_id, "voice_note")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "scheduler_notify_voice_note_failed",
                session_id=session_id,
                error=str(exc),
            )

        # Look up authoritative started_at from the row.
        row = await queries.get_session_by_id(db, session_id)
        started_at = row.started_at if row else 0.0
        voice_note_id = session_id

        # Schedule cap-warning + force-stop (idempotent on the scheduler).
        try:
            await scheduler.schedule_voice_note_caps(
                session_id,
                started_at,
                config.voice_note.max_duration_seconds,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "voice_note_cap_schedule_failed",
                session_id=session_id,
                error=str(exc),
            )

    _set_active(
        request,
        {
            "voice_note_id": voice_note_id,
            "session_id": session_id,
            "started_at": started_at,
            "is_excerpt": is_excerpt,
            "parent_session_id": parent_session_id,
            "max_duration_seconds": config.voice_note.max_duration_seconds,
            "triggered_by": req.triggered_by,
        },
    )

    return VoiceNoteStartResponse(
        voice_note_id=voice_note_id,
        session_id=session_id,
        started_at=started_at,
        is_excerpt=is_excerpt,
        parent_session_id=parent_session_id,
        max_duration_seconds=config.voice_note.max_duration_seconds,
    )


@router.post(
    "/voice-note/stop",
    response_model=VoiceNoteStopResponse,
)
async def voice_note_stop(request: Request) -> VoiceNoteStopResponse:
    """Stop the active voice note. Synchronously transcribes its chunks.

    Per HELIOS.md §9.8 the voice-note flow runs WhisperX on the
    just-recorded chunks before returning, so the floating save window
    can show the transcript immediately. Wave 2E plumbs this through
    ``TranscriptionWorker.transcribe_synchronously``.

    Excerpt mode: chunks live on the parent meeting session. We pull the
    mic-channel chunks within the voice-note's ``[started_at, ended_at]``
    window. The periodic transcription worker may already have processed
    some of them; ``transcribe_synchronously`` reuses those segments
    instead of re-running the model.
    """
    state = _active(request)
    if state is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "voice_note_not_active",
            "no voice note currently recording",
        )

    orch = request.app.state.orchestrator
    scheduler = request.app.state.scheduler
    db = request.app.state.db_pool.writer
    transcription_worker = getattr(
        request.app.state, "transcription_worker", None
    )

    session_id: int = state["session_id"]
    voice_note_id: int = state["voice_note_id"]
    is_excerpt: bool = state["is_excerpt"]
    started_at: float = state["started_at"]

    if not is_excerpt:
        # Cancel the cap timers — successful stop, not a force-stop.
        try:
            await scheduler.cancel_voice_note_caps(session_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "voice_note_cap_cancel_failed",
                session_id=session_id,
                error=str(exc),
            )
        try:
            await orch.stop_session(session_id, reason="voice_note_user_stop")
        except ValueError as exc:
            # Session already gone — clear state, return what we know.
            log.info(
                "voice_note_stop_already_inactive",
                session_id=session_id,
                error=str(exc),
            )
        try:
            await scheduler.notify_session_stopped(session_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "scheduler_notify_voice_note_stopped_failed",
                session_id=session_id,
                error=str(exc),
            )

    # Pull authoritative ended_at when we own the row.
    ended_at = _now(request)
    if not is_excerpt:
        row = await queries.get_session_by_id(db, session_id)
        if row is not None and row.ended_at is not None:
            ended_at = row.ended_at

    duration = max(0.0, ended_at - started_at)
    _set_active(request, None)

    # Resolve the chunks that belong to this voice note. Excerpt mode
    # filters the parent session's chunks by [started_at, ended_at]
    # (mic only — system chunks are diarised separately). Standalone
    # mode pulls all chunks for the dedicated session.
    transcript_response: TranscriptResponse | None = None
    if transcription_worker is not None:
        all_chunks = await queries.get_session_audio_chunks(db, session_id)
        if is_excerpt:
            # ``start_ts`` / ``end_ts`` are REAL (float seconds since
            # epoch) per migrations/001_initial.sql. Sub-second precision
            # is preserved, so this overlap test is exact down to the
            # float resolution of ``time.time()`` — no millisecond /
            # second mismatch concern.
            chunks = [
                c
                for c in all_chunks
                if c.channel == "mic"
                and c.start_ts < ended_at
                and c.end_ts > started_at
                and c.path is not None
            ]
        else:
            chunks = [c for c in all_chunks if c.path is not None]
        try:
            result = await transcription_worker.transcribe_synchronously(chunks)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "voice_note_sync_transcription_failed",
                session_id=session_id,
                voice_note_id=voice_note_id,
                error=str(exc),
            )
            result = None
        if result is not None:
            transcript_response = _build_transcript_response(
                session_id=session_id,
                started_at=started_at,
                ended_at=ended_at,
                segments=result.segments,
            )

    return VoiceNoteStopResponse(
        voice_note_id=voice_note_id,
        session_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        transcript=transcript_response,
        is_excerpt=is_excerpt,
    )


def _build_transcript_response(
    *,
    session_id: int,
    started_at: float,
    ended_at: float,
    segments: list[Any],
) -> TranscriptResponse:
    """Convert ``TranscribedSegment`` objects to the wire schema."""
    seg_responses = [
        TranscriptSegmentResponse(
            start=s.start_ts,
            end=s.end_ts,
            speaker=s.speaker,
            text=s.text,
            words=None,  # word-level expansion not surfaced in voice-note stop
        )
        for s in segments
    ]
    return TranscriptResponse(
        session_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        segments=seg_responses,
        coverage=TranscriptCoverage(
            captured_seconds=max(0.0, ended_at - started_at),
        ),
        diarization_status="not_applicable",
    )


@router.post(
    "/voice-note/cancel",
    response_model=VoiceNoteCancelResponse,
)
async def voice_note_cancel(request: Request) -> VoiceNoteCancelResponse:
    """Discard the active voice note (no transcription)."""
    state = _active(request)
    if state is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "voice_note_not_active",
            "no voice note currently recording",
        )

    orch = request.app.state.orchestrator
    scheduler = request.app.state.scheduler

    session_id: int = state["session_id"]
    voice_note_id: int = state["voice_note_id"]
    is_excerpt: bool = state["is_excerpt"]

    if not is_excerpt:
        try:
            await scheduler.cancel_voice_note_caps(session_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "voice_note_cap_cancel_failed_on_cancel",
                session_id=session_id,
                error=str(exc),
            )
        try:
            await orch.stop_session(session_id, reason="voice_note_cancelled")
        except ValueError as exc:
            log.info(
                "voice_note_cancel_already_inactive",
                session_id=session_id,
                error=str(exc),
            )
        try:
            await scheduler.notify_session_stopped(session_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "scheduler_notify_voice_note_cancel_failed",
                session_id=session_id,
                error=str(exc),
            )

    _set_active(request, None)
    return VoiceNoteCancelResponse(cancelled_voice_note_id=voice_note_id)


@router.get(
    "/voice-note/active",
    response_model=ActiveVoiceNoteResponse,
)
async def voice_note_active(request: Request) -> ActiveVoiceNoteResponse:
    """Current voice-note state, used by menu bar to recover after a crash.

    Track 4G: also computes ``current_rms`` from the trailing 250ms of the
    most recent mic-channel chunk on the active voice-note's session.
    The floating indicator polls this every 250ms to drive its level bar.
    """
    state = _active(request)
    if state is None:
        return ActiveVoiceNoteResponse(active=None)

    now = _now(request)
    elapsed = max(0.0, now - state["started_at"])
    max_duration = float(state["max_duration_seconds"])
    approaching = elapsed >= (max_duration - _APPROACHING_CAP_SECONDS)

    # Compute the current RMS from the most-recent mic chunk. Best-effort:
    # any failure (no chunks yet, file missing, header weird) returns 0.0.
    current_rms = 0.0
    try:
        db = request.app.state.db_pool.writer
        chunks = await queries.get_session_audio_chunks(db, state["session_id"])
        # Filter to mic-channel chunks with a path on disk — system-audio
        # chunks aren't relevant to the user-facing voice-level meter, and
        # rows without a path are no-audio markers.
        mic_chunks = [
            c for c in chunks if c.channel == "mic" and c.path is not None
        ]
        if mic_chunks:
            # ``get_session_audio_chunks`` returns oldest-first; the
            # last entry is the most recent.
            current_rms = _compute_recent_rms(mic_chunks[-1].path)
    except Exception as exc:  # noqa: BLE001 — RMS is best-effort
        log.debug(
            "voice_note_active_rms_failed",
            session_id=state.get("session_id"),
            error=str(exc),
        )

    return ActiveVoiceNoteResponse(
        active=ActiveVoiceNote(
            voice_note_id=state["voice_note_id"],
            session_id=state["session_id"],
            started_at=state["started_at"],
            elapsed_seconds=elapsed,
            is_excerpt=state["is_excerpt"],
            max_duration_seconds=int(max_duration),
            approaching_cap=approaching,
            current_rms=current_rms,
        )
    )
