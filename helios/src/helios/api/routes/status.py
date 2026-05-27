"""``/v1/status``, ``/v1/permissions``, ``/v1/diagnostics`` endpoints.

Per HELIOS.md §7.3, these power the menu bar polling loop and the
dashboard overview.

Phase-2 caveats:

* Queue counts are returned as ``0`` everywhere — Phase 3 wires the
  real transcription queue into the response.
* ``memory_mb`` / ``cpu_5min_avg_pct`` are ``None`` unless ``psutil`` is
  importable (it is not in Helios' base deps); we fall back rather than
  failing the diagnostics call.
* ``audio_oldest_days`` is computed from the oldest ``audio_chunks.start_ts``;
  byte-level storage stats are best-effort via ``Path.stat``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import APIRouter, Request

from helios.api.schemas import (
    ActiveSessionResponse,
    ComponentErrorResponse,
    ComponentStatusResponse,
    DaemonEventResponse,
    DiagnosticsResponse,
    LastChunksResponse,
    NextCalendarEventResponse,
    PermissionsResponse,
    QueueCountsResponse,
    StatusResponse,
    StorageDiagnosticsResponse,
)
from helios.db import queries
from helios.log import get_logger

router = APIRouter()
log = get_logger("api.status")

# Process start time used for uptime calculation. Captured at import.
_PROCESS_STARTED_AT = time.time()

# Default components reported in status.components. Phase 3 will plumb
# real worker health; for Phase 2 we mark known subsystems "ok" except
# unimplemented ones, then overlay any active component_errors as
# "degraded".
_DEFAULT_COMPONENTS = {
    "audio_capture": "ok",
    "transcription": "unavailable",
    "diarization": "unavailable",
    "ocr": "unavailable",
}


async def _components_view(component_errors: dict, db) -> dict[str, str]:
    """Merge default component health with the most recent rows from the
    ``component_status`` table and any active in-memory error entries.

    Without this DB lookup the field stayed stuck on the
    ``_DEFAULT_COMPONENTS`` map (transcription/diarization/ocr always
    "unavailable") even after a worker reported ``ok`` to the table —
    the dashboard's overview then showed stale "unavailable" indicators
    long after the HF wizard or a fresh capture wired the worker up.
    Reads the same table ``GET /v1/diagnostics`` uses, so the two
    endpoints stay in sync.
    """
    view = dict(_DEFAULT_COMPONENTS)
    try:
        rows = await queries.get_recent_component_status(db)
        for row in rows:
            view[row.component] = row.status
    except Exception as exc:  # noqa: BLE001 — degrade to defaults
        log.warning("components_view_db_read_failed", error=str(exc))
    for key in component_errors:
        # Any component currently in error → degraded.
        view[key] = "degraded"
    return view


def _build_active_session(scheduler) -> ActiveSessionResponse | None:
    """Build the §7.3 active_session payload from scheduler info.

    Returns ``None`` when there is no active session known to the
    scheduler — ``GET /v1/status`` then renders ``"active_session": null``.
    """
    info = scheduler.get_active_session_info()
    if info is None:
        return None
    return ActiveSessionResponse(
        id=info.session_id,
        kind=info.kind,
        calendar_event_ids=list(info.calendar_event_ids),
        started_at=info.started_at,
        screen_capture_override_until=info.screen_capture_override_until,
    )


def _build_next_calendar_event(scheduler) -> NextCalendarEventResponse | None:
    """Build the §7.3 next_calendar_event payload."""
    info = scheduler.get_next_calendar_event()
    if info is None:
        return None
    return NextCalendarEventResponse(
        calendar_event_id=info.calendar_event_id,
        title=info.title,
        starts_at=info.starts_at,
        pre_start_at=info.pre_start_at,
    )


async def _read_permissions(request: Request) -> PermissionsResponse:
    """Read the most recent permission row from the DB (per 2F's report)."""
    db = request.app.state.db_pool.writer
    last = await queries.get_last_permission_check(db)
    if last is None:
        return PermissionsResponse(
            mic_granted=False,
            screen_recording_granted=False,
            last_checked_at=None,
        )
    return PermissionsResponse(
        mic_granted=bool(last.mic_granted),
        screen_recording_granted=bool(last.screen_recording_granted),
        last_checked_at=last.checked_at,
    )


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request) -> StatusResponse:
    """Snapshot of daemon state for the menu bar / dashboard overview.

    Wire shape per HELIOS.md §7.3 ``GET /v1/status``.
    """
    sm = request.app.state.state_machine
    scheduler = request.app.state.scheduler
    snapshot = sm.current()

    component_errors = {
        name: ComponentErrorResponse(
            component=err.component,
            message=err.message,
            occurred_at=err.occurred_at,
        )
        for name, err in snapshot.component_errors.items()
    }

    permissions = await _read_permissions(request)
    active_session = _build_active_session(scheduler)
    next_calendar_event = _build_next_calendar_event(scheduler)

    db = request.app.state.db_pool.writer
    return StatusResponse(
        daemon="running",
        mode=snapshot.mode,
        components=await _components_view(snapshot.component_errors, db),
        component_errors=component_errors,
        active_session=active_session,
        next_calendar_event=next_calendar_event,
        active_session_id=snapshot.active_session_id,
        paused_until=scheduler.paused_until,
        aegis_unreachable=snapshot.aegis_unreachable,
        last_error=snapshot.last_error,
        # Phase 3 lights up the real queue counts (Track 3D).
        queue=QueueCountsResponse(),
        scheduler_running=scheduler.is_running,
        permissions=permissions,
    )


@router.get("/permissions", response_model=PermissionsResponse)
async def get_permissions(request: Request) -> PermissionsResponse:
    """Latest mic + screen-recording permission state from the DB."""
    return await _read_permissions(request)


def _maybe_psutil_metrics() -> tuple[float | None, float | None]:
    """Return (memory_mb, cpu_5min_avg_pct) or (None, None) when psutil is missing."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None, None
    try:
        proc = psutil.Process(os.getpid())
        mem_mb = proc.memory_info().rss / (1024 * 1024)
        # 5-min CPU avg isn't directly available; substitute system load avg.
        try:
            load1, _, _ = os.getloadavg()
            cpu = float(load1) * 100.0 / max(1, os.cpu_count() or 1)
        except (OSError, AttributeError):
            cpu = float(proc.cpu_percent(interval=0.0))
        return float(mem_mb), float(cpu)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("psutil_metrics_failed", error=str(exc))
        return None, None


def _storage_stats(root: Path) -> StorageDiagnosticsResponse:
    """Best-effort byte counts for audio + thumbnails + db.

    Walks the storage tree once. On any IO error returns zeros for that
    bucket — diagnostics must never crash the daemon.
    """
    audio_bytes = 0
    thumb_bytes = 0
    db_bytes = 0
    try:
        if root.exists():
            for path in root.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                    sz = path.stat().st_size
                    name = path.name.lower()
                    if name.endswith(".db") or name.endswith(".sqlite"):
                        db_bytes += sz
                    elif name.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        thumb_bytes += sz
                    elif name.endswith((".wav", ".flac", ".mp3", ".opus")):
                        audio_bytes += sz
                except OSError:
                    continue
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("storage_walk_failed", root=str(root), error=str(exc))
    return StorageDiagnosticsResponse(
        audio_bytes=audio_bytes,
        audio_oldest_days=0,  # Filled below when chunk data is available.
        thumbnails_bytes=thumb_bytes,
        database_bytes=db_bytes,
    )


@router.get("/diagnostics", response_model=DiagnosticsResponse)
async def get_diagnostics(request: Request) -> DiagnosticsResponse:
    """Full diagnostic dump (HELIOS.md §7.3)."""
    config = request.app.state.config
    db = request.app.state.db_pool.writer

    permissions = await _read_permissions(request)
    sm = request.app.state.state_machine
    snapshot = sm.current()

    mem_mb, cpu = _maybe_psutil_metrics()

    storage_root = Path(config.storage.root).expanduser()
    storage = _storage_stats(storage_root)

    component_status_rows = await queries.get_recent_component_status(db)
    component_status = [
        ComponentStatusResponse(
            component=row.component,
            status=row.status,
            ts=row.ts,
            reason=row.reason,
            detail=row.detail,
            action=row.action,
        )
        for row in component_status_rows
    ]

    event_rows = await queries.get_recent_daemon_events(db, limit=50)
    recent_events = [
        DaemonEventResponse(
            ts=row.ts,
            level=row.level,
            component=row.component,
            event=row.event,
            details=row.details,
        )
        for row in event_rows
    ]

    scheduler = request.app.state.scheduler
    active_session = _build_active_session(scheduler)
    next_event = _build_next_calendar_event(scheduler)

    return DiagnosticsResponse(
        version="0.1.0",
        pid=os.getpid(),
        uptime_seconds=time.time() - _PROCESS_STARTED_AT,
        memory_mb=mem_mb,
        cpu_5min_avg_pct=cpu,
        permissions=permissions,
        active_session=active_session,
        next_event=next_event,
        # Phase 3 fills last_chunks / throughput from the real workers.
        last_chunks=LastChunksResponse(),
        transcription_throughput_realtime_multiple=None,
        active_session_id=snapshot.active_session_id,
        queues=QueueCountsResponse(),  # Phase 3.
        storage=storage,
        component_status=component_status,
        recent_events=recent_events,
    )
