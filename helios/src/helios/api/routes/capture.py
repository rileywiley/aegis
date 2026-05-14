"""Capture session HTTP API.

Per HELIOS.md §7.3, this module exposes everything under
``/v1/capture/*``:

* ``POST /capture/start`` — manual session start (continuous /
  manual_screen)
* ``POST /capture/stop`` — idempotent session stop
* ``POST /capture/pause-until`` — schedule a pause window
* ``POST /capture/resume`` — clear the pause window
* ``POST /capture/enable-screen-override`` — Phase-5 OCR override hook;
  Phase-2 logs and accepts.
* ``POST /capture/prompt-response`` — user response to the 4-hour
  continuous-mode prompt.

Bearer auth is enforced at the router level
(``include_router(..., dependencies=[Depends(require_token)])``) — see
:mod:`helios.api`.

Scheduler hooks are mandatory: every API-triggered start/stop must call
``scheduler.notify_session_started`` / ``notify_session_stopped`` so the
4-hour prompt + 5:30 PM hard stop apply to API-started sessions.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from helios.api.schemas import (
    CapturePauseUntilRequest,
    CapturePauseUntilResponse,
    CaptureResumeResponse,
    CaptureScreenOverrideRequest,
    CaptureScreenOverrideResponse,
    CaptureStartRequest,
    CaptureStartResponse,
    CaptureStopResponse,
    PromptResponseRequest,
    PromptResponseResponse,
)
from helios.log import get_logger

router = APIRouter()
log = get_logger("api.capture")


def _err(code: int, error: str, detail: str) -> HTTPException:
    """Build a §7.2 error envelope wrapped in HTTPException."""
    return HTTPException(
        status_code=code,
        detail={"error": error, "detail": detail, "code": code},
    )


@router.post(
    "/capture/start",
    response_model=CaptureStartResponse,
    responses={409: {"description": "session already active"}},
)
async def start_capture(
    req: CaptureStartRequest, request: Request
) -> CaptureStartResponse:
    """Start a manual capture session.

    The §7.3 wire contract restricts ``kind`` to ``continuous`` /
    ``manual_screen``. ``manual_screen`` requires ``duration_minutes``
    (validated by the schema's model_validator → 422 if missing).

    Hooks the scheduler so 4-hr prompt + 5:30 PM hard stop apply to
    sessions started via the API.
    """
    orch = request.app.state.orchestrator
    scheduler = request.app.state.scheduler
    try:
        session_id = await orch.start_session(kind=req.kind)
    except RuntimeError as exc:
        # Single-active-session rule violated.
        raise _err(status.HTTP_409_CONFLICT, "session_already_active", str(exc))
    except PermissionError as exc:  # pragma: no cover - real backend only
        raise _err(status.HTTP_403_FORBIDDEN, "permission_denied", str(exc))

    # Notify scheduler so hard-stop / 4hr-prompt apply (HELIOS.md §11)
    # AND state-machine flips to mode="recording" (covers /v1/status).
    try:
        await scheduler.notify_session_started(session_id, req.kind)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("scheduler_notify_failed", session_id=session_id, error=str(exc))

    # Prefer the row's started_at (authoritative). Fall back to the
    # app-state Clock if the row is missing (racy "row missing" edge).
    started_at = request.app.state.clock.time()
    from helios.db import queries as _q

    row = await _q.get_session_by_id(request.app.state.db_pool.writer, session_id)
    if row is not None:
        started_at = row.started_at
    return CaptureStartResponse(
        session_id=session_id, kind=req.kind, started_at=started_at
    )


@router.post(
    "/capture/stop",
    response_model=CaptureStopResponse,
)
async def stop_capture(request: Request) -> CaptureStopResponse:
    """Stop the active session — idempotent.

    Per HELIOS.md §7.3 the body is empty and the response includes
    ``session_id: null`` when nothing was active.
    """
    orch = request.app.state.orchestrator
    scheduler = request.app.state.scheduler

    active = orch.active_session_id
    if active is None:
        return CaptureStopResponse(session_id=None, ended_at=None, end_reason=None)

    reason = "user_stop"
    try:
        await orch.stop_session(active, reason=reason)
    except ValueError as exc:
        # Race: session already gone. Still return idempotent shape.
        log.info("stop_already_inactive", error=str(exc))
        return CaptureStopResponse(session_id=None, ended_at=None, end_reason=None)

    try:
        await scheduler.notify_session_stopped(active)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("scheduler_notify_stopped_failed", session_id=active, error=str(exc))

    from helios.db import queries as _q

    row = await _q.get_session_by_id(request.app.state.db_pool.writer, active)
    return CaptureStopResponse(
        session_id=active,
        ended_at=row.ended_at if row else None,
        end_reason=row.end_reason if row else reason,
    )


@router.post(
    "/capture/pause-until",
    response_model=CapturePauseUntilResponse,
)
async def pause_until(
    req: CapturePauseUntilRequest, request: Request
) -> CapturePauseUntilResponse:
    scheduler = request.app.state.scheduler
    await scheduler.set_pause(req.until_ts)
    return CapturePauseUntilResponse(paused_until=req.until_ts)


@router.post(
    "/capture/resume",
    response_model=CaptureResumeResponse,
)
async def resume(request: Request) -> CaptureResumeResponse:
    scheduler = request.app.state.scheduler
    await scheduler.resume()
    return CaptureResumeResponse(paused=False)


@router.post(
    "/capture/enable-screen-override",
    response_model=CaptureScreenOverrideResponse,
    responses={404: {"description": "no active session matches session_id"}},
)
async def enable_screen_override(
    req: CaptureScreenOverrideRequest, request: Request
) -> CaptureScreenOverrideResponse:
    """Enable the screen-capture override window (HELIOS_BUILD_PLAN §5B).

    Flips ``capture_sessions.screen_capture_override_until`` AND the
    scheduler's ``ActiveSessionInfo.screen_capture_override_until`` so
    the value is visible on the very next ``GET /v1/status`` response.
    The OCR worker (Agent 5A) reads the column to decide whether to
    bypass the app allowlist while the override window is in the future.

    Semantics:

    * ``session_id`` omitted → applies to whatever session the
      orchestrator currently has active.
    * No active session matching ``session_id`` → 404.
    * Replace-with-latest: a second call overwrites the prior value even
      if it shortens it (menu bar's fixed slot model — most recent
      click wins).
    """
    orch = request.app.state.orchestrator
    scheduler = request.app.state.scheduler
    clock = request.app.state.clock
    db = request.app.state.db_pool.writer

    duration_seconds = req.effective_duration_seconds

    target_sid = req.session_id if req.session_id is not None else orch.active_session_id
    if target_sid is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "no_active_session",
            "no capture session is currently active",
        )

    # Require the requested session id to match the orchestrator's
    # active session — Phase 1 enforces a single-active-session rule,
    # and an override for a stopped row would have no consumer.
    if orch.active_session_id != target_sid:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_active",
            f"session {target_sid} is not the active capture session",
        )

    until_ts = float(clock.time()) + float(duration_seconds)

    # Update in-memory scheduler view FIRST so a concurrent /v1/status
    # call always sees a value at least as up-to-date as the DB row.
    scheduler_until = scheduler.set_screen_capture_override(target_sid, until_ts)
    if scheduler_until is None:
        # Scheduler doesn't know about this session (rare — would mean
        # orchestrator + scheduler bookkeeping diverged). Fail loudly so
        # we don't silently miss the override window.
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_tracked",
            f"scheduler has no record of session {target_sid}",
        )

    # Persist to the capture_sessions row so post-restart reads see it.
    from helios.db import queries as _q

    persisted = await _q.update_session_screen_override(
        db, target_sid, scheduler_until
    )
    if not persisted:
        # Row missing — shouldn't happen because the scheduler had it,
        # but surface as a 404 instead of a 500.
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            f"session {target_sid} not found in database",
        )

    duration_minutes = max(1, int(round(duration_seconds / 60)))
    log.info(
        "screen_override_enabled",
        session_id=target_sid,
        duration_seconds=duration_seconds,
        screen_capture_override_until=scheduler_until,
    )
    return CaptureScreenOverrideResponse(
        ok=True,
        session_id=target_sid,
        duration_minutes=duration_minutes,
        duration_seconds=int(duration_seconds),
        screen_capture_override_until=scheduler_until,
    )


@router.post(
    "/capture/prompt-response",
    response_model=PromptResponseResponse,
)
async def prompt_response(
    req: PromptResponseRequest, request: Request
) -> PromptResponseResponse:
    """User answer to the 4-hour continuous-mode prompt."""
    scheduler = request.app.state.scheduler
    await scheduler.respond_to_prompt(req.continue_session)
    return PromptResponseResponse(ok=True)
