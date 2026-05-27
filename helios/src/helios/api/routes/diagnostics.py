"""``/v1/diagnostics/*`` action endpoints (Wave 6D — Track 6D).

Replaces the Phase-2 stubs:

* ``POST /diagnostics/restart`` — SIGTERMs the daemon (LaunchAgent
  ``KeepAlive=true`` respawns it).
* ``POST /diagnostics/flush-queues`` — drains pending transcription +
  diarization work (returns row counts).
* ``POST /diagnostics/test-capture`` — runs the 60s self-test in a
  background task, returns a ``job_id`` for polling.
* ``GET  /diagnostics/test-capture/{job_id}`` — poll endpoint for the
  self-test runner.
* ``POST /diagnostics/reload-component`` — stops + restarts a worker.
* ``POST /diagnostics/bundle`` — builds the tar.gz bundle (§13.11) and
  returns a download URL.
* ``GET  /diagnostics/bundle/{filename}`` — streams the bundle file.

All long-running work runs in a background task so the HTTP response
returns immediately. The self-test result lives on
``app.state.self_test_jobs`` (in-process dict) — the spec allows that
for a one-shot diagnostic and avoids a schema migration.

Never logs raw transcript text, OCR text, or bearer tokens.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import signal
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Path as FsPath,
    Request,
    status,
)
from fastapi.responses import FileResponse

from helios.api.schemas import (
    BundleResponse,
    DiagnosticsAcceptedResponse,
    FlushQueuesResponse,
    ReloadComponentRequest,
    ReloadComponentResponse,
    TestCaptureStartResponse,
    TestCaptureStatusResponse,
    TestCaptureStep,
)
from helios.db import queries
from helios.log import get_logger
from helios.workers.self_test import SelfTestRunner

router = APIRouter()
log = get_logger("api.diagnostics_actions")


# Bundle staging directory. One process-global path so the cleanup
# scanner has a single root to walk.
_BUNDLE_DIR_NAME = "diagnostic_bundles"
_BUNDLE_TTL_SECONDS = 3600.0  # 1 hour per §13.11


def _err(code: int, error: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"error": error, "detail": detail, "code": code},
    )


def _bundle_dir(storage_root: Path) -> Path:
    """Resolve the bundle staging directory under storage_root."""
    return Path(storage_root).expanduser() / _BUNDLE_DIR_NAME


def _cleanup_old_bundles(bundle_dir: Path, ttl_seconds: float) -> int:
    """Remove bundle files older than ``ttl_seconds``. Best-effort."""
    if not bundle_dir.exists():
        return 0
    now = time.time()
    removed = 0
    try:
        for path in bundle_dir.iterdir():
            try:
                if not path.is_file():
                    continue
                mtime = path.stat().st_mtime
                if (now - mtime) > ttl_seconds:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


# ---------------------------------------------------------------------------
# Restart — SIGTERM self after responding.
# ---------------------------------------------------------------------------


def _suicide() -> None:
    """Fire SIGTERM at our own PID. LaunchAgent respawns the daemon."""
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("self_sigterm_failed", error=str(exc))


async def _delayed_suicide(delay: float = 0.5) -> None:
    await asyncio.sleep(delay)
    _suicide()


@router.post(
    "/diagnostics/restart",
    response_model=DiagnosticsAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restart_daemon(
    background_tasks: BackgroundTasks, request: Request
) -> DiagnosticsAcceptedResponse:
    """Issue SIGTERM to self after the HTTP response is flushed.

    LaunchAgent ``KeepAlive=true`` respawns the daemon.

    Test harness escape hatch: set
    ``request.app.state.suppress_restart_sigterm = True`` to skip the
    signal. The endpoint still returns 202; tests can then assert the
    counter on ``app.state.restart_calls`` (incremented unconditionally).
    """
    log.info("daemon_restart_requested")
    request.app.state.restart_calls = (
        getattr(request.app.state, "restart_calls", 0) + 1
    )
    if not getattr(request.app.state, "suppress_restart_sigterm", False):
        background_tasks.add_task(_delayed_suicide, 0.5)
    return DiagnosticsAcceptedResponse(
        detail="restart signal scheduled; LaunchAgent will respawn"
    )


# ---------------------------------------------------------------------------
# Flush queues — sync count of rows affected.
# ---------------------------------------------------------------------------


@router.post(
    "/diagnostics/flush-queues",
    response_model=FlushQueuesResponse,
)
async def flush_queues(request: Request) -> FlushQueuesResponse:
    """Drain the pending transcription + diarization queues.

    Synchronous: returns the row counts in the same request. The
    transcription worker's in-memory queue isn't directly drainable
    (it consults the DB on each poll), so flushing the DB state is
    equivalent: the next poll picks up nothing.
    """
    db = request.app.state.db_pool.writer
    tx = await queries.delete_pending_transcription_chunks(db)
    diar = await queries.delete_pending_diarization_jobs(db)
    log.info(
        "queue_flush_complete",
        transcription_flushed=tx,
        diarization_flushed=diar,
    )
    return FlushQueuesResponse(
        transcription_flushed=tx,
        diarization_flushed=diar,
    )


# ---------------------------------------------------------------------------
# Test capture — async job with in-memory progress.
# ---------------------------------------------------------------------------


def _get_jobs_dict(request: Request) -> dict[str, Any]:
    """Lazily initialise the self-test jobs dict on app.state."""
    if not hasattr(request.app.state, "self_test_jobs"):
        request.app.state.self_test_jobs = {}
    return request.app.state.self_test_jobs


async def _run_self_test_job(
    request_app_state: Any,
    job_id: str,
    runner: SelfTestRunner,
) -> None:
    """Background task wrapper that updates the job entry in-place."""
    try:
        await runner.run()
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "self_test_unexpected_failure",
            job_id=job_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        runner.result.status = "failed"
        runner.result.finished_at = time.time()
    request_app_state.self_test_jobs[job_id] = runner.result


@router.post(
    "/diagnostics/test-capture",
    response_model=TestCaptureStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def test_capture(
    background_tasks: BackgroundTasks, request: Request
) -> TestCaptureStartResponse:
    """Schedule the 60-second self-test (HELIOS_BUILD_PLAN §6D.2).

    Returns immediately with a ``job_id``. Poll
    ``GET /v1/diagnostics/test-capture/{job_id}`` for progress.
    """
    jobs = _get_jobs_dict(request)

    config = request.app.state.config
    storage_root = Path(config.storage.root).expanduser()
    capture_seconds = float(
        getattr(request.app.state, "self_test_capture_seconds", 60.0)
    )
    transcribe_timeout_seconds = float(
        getattr(
            request.app.state,
            "self_test_transcribe_timeout_seconds",
            30.0,
        )
    )

    runner = SelfTestRunner(
        db=request.app.state.db_pool.writer,
        orchestrator=request.app.state.orchestrator,
        config=config,
        storage_root=storage_root,
        transcription_worker=getattr(
            request.app.state, "transcription_worker", None
        ),
        diarization_worker=getattr(
            request.app.state, "diarization_worker", None
        ),
        capture_seconds=capture_seconds,
        transcribe_timeout_seconds=transcribe_timeout_seconds,
    )
    # Seed the jobs dict with the queued placeholder so the polling
    # endpoint returns a 200 even if the user races the background task.
    jobs[runner.result.job_id] = runner.result

    log.info("self_test_requested", job_id=runner.result.job_id)
    background_tasks.add_task(
        _run_self_test_job, request.app.state, runner.result.job_id, runner
    )
    return TestCaptureStartResponse(
        job_id=runner.result.job_id,
        status="queued",
    )


@router.get(
    "/diagnostics/test-capture/{job_id}",
    response_model=TestCaptureStatusResponse,
)
async def get_test_capture_status(
    request: Request,
    job_id: str = FsPath(..., min_length=1),
) -> TestCaptureStatusResponse:
    """Poll the status of a self-test job.

    Returns 404 once a job's result has aged out of the in-memory dict
    (no current age-out logic — kept for the lifetime of the process).
    """
    jobs = _get_jobs_dict(request)
    result = jobs.get(job_id)
    if result is None:
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "job_not_found",
            f"self-test job {job_id!r} not found",
        )
    return TestCaptureStatusResponse(
        job_id=result.job_id,
        status=result.status,  # type: ignore[arg-type]
        started_at=result.started_at,
        finished_at=result.finished_at,
        steps=[
            TestCaptureStep(name=s.name, ok=s.ok, detail=s.detail)
            for s in result.steps
        ],
        session_id=result.session_id,
    )


# ---------------------------------------------------------------------------
# Reload component — stop + start a worker.
# ---------------------------------------------------------------------------


_COMPONENT_ATTR_MAP = {
    "transcription": "transcription_worker",
    "diarization": "diarization_worker",
    "ocr": "active_ocr_worker",  # per-session, may be None
    "merge": "merge_worker",
    "cleanup": "cleanup_worker",
}


async def _reload_worker(worker: Any) -> None:
    """Stop then start a worker. Stop is best-effort."""
    if worker is None:
        return
    try:
        await worker.stop()
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning("reload_stop_failed", error=str(exc))
    await worker.start()


@router.post(
    "/diagnostics/reload-component",
    response_model=ReloadComponentResponse,
)
async def reload_component(
    req: ReloadComponentRequest, request: Request
) -> ReloadComponentResponse:
    """Restart a single component (e.g. after user installs ffmpeg)."""
    name = req.component.strip().lower()
    attr = _COMPONENT_ATTR_MAP.get(name)
    if attr is None:
        log.info("component_reload_unknown", component=name)
        return ReloadComponentResponse(
            component=name,
            ok=False,
            detail=(
                f"unknown component: {name!r}. "
                f"supported: {sorted(_COMPONENT_ATTR_MAP)}"
            ),
        )
    # OCR worker is per-session and lives on the orchestrator.
    if name == "ocr":
        orch = request.app.state.orchestrator
        worker = getattr(orch, "_active_ocr_worker", None)
        if worker is None:
            return ReloadComponentResponse(
                component=name,
                ok=True,
                detail="no active OCR worker; next session will start fresh",
            )
    else:
        worker = getattr(request.app.state, attr, None)
        if worker is None:
            return ReloadComponentResponse(
                component=name,
                ok=False,
                detail=f"{attr} not wired on app.state",
            )

    log.info("component_reload_requested", component=name)
    try:
        await _reload_worker(worker)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "component_reload_failed",
            component=name,
            error=str(exc),
        )
        return ReloadComponentResponse(
            component=name,
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return ReloadComponentResponse(
        component=name, ok=True, detail="restarted"
    )


# ---------------------------------------------------------------------------
# Bundle (§13.11) — creates tar.gz with diagnostic data.
# ---------------------------------------------------------------------------


def _format_copy_diagnostics(diag: dict[str, Any]) -> str:
    """Build the ``diagnostics.txt`` block from a /v1/diagnostics dict.

    Mirrors HELIOS.md §13.10's plaintext format. Sensitive values are
    not present in the diagnostics response so no further redaction is
    needed here.
    """
    lines: list[str] = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    lines.append(f"Helios Diagnostics — {ts}")
    lines.append(f"Version: {diag.get('version', 'unknown')}")
    uptime = float(diag.get("uptime_seconds") or 0.0)
    lines.append(f"Daemon PID: {diag.get('pid', '?')} (uptime {int(uptime)}s)")
    if diag.get("memory_mb") is not None:
        lines.append(f"Memory: {diag['memory_mb']:.0f} MB")
    if diag.get("cpu_5min_avg_pct") is not None:
        lines.append(f"CPU (5min avg): {diag['cpu_5min_avg_pct']:.0f}%")
    lines.append("")
    perms = diag.get("permissions") or {}
    lines.append("Permissions:")
    lines.append(
        f"  Microphone: {'granted' if perms.get('mic_granted') else 'denied'}"
    )
    lines.append(
        f"  Screen Recording: "
        f"{'granted' if perms.get('screen_recording_granted') else 'denied'}"
    )
    lines.append("")
    lines.append("Components:")
    for row in diag.get("component_status") or []:
        lines.append(
            f"  {row.get('component', '?')}: {row.get('status', '?')}"
        )
    lines.append("")
    queues = diag.get("queues") or {}
    lines.append("Queues:")
    lines.append(
        f"  Transcription: {queues.get('transcription_pending', 0)} pending, "
        f"{queues.get('transcription_failed_24h', 0)} failed (24h)"
    )
    lines.append(
        f"  Diarization:   {queues.get('diarization_pending', 0)} pending, "
        f"{queues.get('diarization_failed_24h', 0)} failed (24h)"
    )
    lines.append("")
    storage = diag.get("storage") or {}
    lines.append("Disk:")
    lines.append(f"  Audio: {storage.get('audio_bytes', 0)} bytes")
    lines.append(f"  Thumbnails: {storage.get('thumbnails_bytes', 0)} bytes")
    lines.append(f"  Database: {storage.get('database_bytes', 0)} bytes")
    lines.append("")
    lines.append("Recent Events (last 20):")
    for ev in (diag.get("recent_events") or [])[:20]:
        lines.append(
            f"  ts={ev.get('ts', 0):.0f}  "
            f"{ev.get('level', '?'):5s}  "
            f"{ev.get('component', '?')}  "
            f"{ev.get('event', '?')}"
        )
    return "\n".join(lines) + "\n"


def _redact_config_toml(toml_text: str) -> str:
    """Replace bearer_token + hf_token values with ``"<redacted>"``."""
    out_lines: list[str] = []
    for line in toml_text.splitlines():
        stripped = line.lstrip()
        for key in ("bearer_token", "hf_token", "token"):
            if stripped.startswith(f"{key} =") or stripped.startswith(
                f"{key}="
            ):
                # Preserve indentation prefix.
                indent_len = len(line) - len(stripped)
                line = " " * indent_len + f'{key} = "<redacted>"'
                break
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def _system_info_text() -> str:
    """Gather macOS system info via small subprocess calls.

    Each call is wrapped in try/except so the bundle never fails because
    of an unavailable utility (handy for CI / Linux dev).
    """
    parts: list[str] = []

    def _run(cmd: list[str]) -> str:
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return (out.stdout or "").strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return f"<unavailable: {type(exc).__name__}: {exc}>"

    parts.append("=== sw_vers ===")
    parts.append(_run(["sw_vers"]))
    parts.append("")
    parts.append("=== Hardware ===")
    parts.append(_run(["system_profiler", "SPHardwareDataType"]))
    parts.append("")
    parts.append("=== Audio Devices ===")
    parts.append(_run(["system_profiler", "SPAudioDataType"]))
    parts.append("")
    parts.append("=== Displays ===")
    parts.append(_run(["system_profiler", "SPDisplaysDataType"]))
    return "\n".join(parts) + "\n"


def _read_log_tail_gzipped(
    log_path: Path, max_bytes: int = 5 * 1024 * 1024
) -> bytes:
    """Read up to ``max_bytes`` from the tail of helios.log, gzipped.

    Returns an empty gzip stream when the log file is missing — keeps
    the bundle layout consistent regardless.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        try:
            if log_path.exists() and log_path.is_file():
                size = log_path.stat().st_size
                with log_path.open("rb") as f:
                    if size > max_bytes:
                        f.seek(size - max_bytes)
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        gz.write(chunk)
            else:
                gz.write(b"<helios.log not found>\n")
        except OSError as exc:
            gz.write(f"<log read error: {exc}>\n".encode())
    return buf.getvalue()


def _build_diagnostics_dict_sync_safe(
    diag_response: Any,
) -> dict[str, Any]:
    """Best-effort conversion of the DiagnosticsResponse to a plain dict."""
    if hasattr(diag_response, "model_dump"):
        return diag_response.model_dump()
    if hasattr(diag_response, "dict"):
        return diag_response.dict()
    return dict(diag_response)


async def _build_bundle_async(
    request: Request,
    bundle_path: Path,
) -> int:
    """Build the tar.gz at ``bundle_path``. Returns its size in bytes."""
    # 1) Pull the live diagnostics snapshot. We import lazily to avoid a
    #    circular import (status routes depend on schemas).
    from helios.api.routes.status import get_diagnostics

    diag_response = await get_diagnostics(request)
    diag_dict = _build_diagnostics_dict_sync_safe(diag_response)
    diagnostics_txt = _format_copy_diagnostics(diag_dict)

    # 2) Pull last 100 daemon_events as JSON. ``daemon_events`` exists
    #    in migration 001 so we can rely on it.
    db = request.app.state.db_pool.writer
    events_rows = await queries.get_recent_daemon_events_json(db, limit=100)
    events_json = json.dumps(events_rows, indent=2, default=str)

    # 3) Read + redact config.toml.
    from helios import config as helios_config

    config_path = helios_config._CONFIG_PATH  # type: ignore[attr-defined]
    try:
        raw_toml = Path(config_path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raw_toml = f"# <unavailable: {exc}>\n"
    redacted_toml = _redact_config_toml(raw_toml)

    # 4) Log tail (gzipped).
    log_path = Path("~/.aegis/capture/logs/helios.log").expanduser()
    log_gz = _read_log_tail_gzipped(log_path)

    # 5) System info.
    system_txt = _system_info_text()

    # 6) Stream-write tar.gz to the destination.
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, "w:gz") as tar:

        def _add_bytes(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))

        _add_bytes("diagnostics.txt", diagnostics_txt.encode("utf-8"))
        _add_bytes("events.json", events_json.encode("utf-8"))
        _add_bytes("config.toml.redacted", redacted_toml.encode("utf-8"))
        _add_bytes("system.txt", system_txt.encode("utf-8"))
        _add_bytes("logs/helios.log.gz", log_gz)

    return bundle_path.stat().st_size


@router.post(
    "/diagnostics/bundle",
    response_model=BundleResponse,
)
async def create_diagnostic_bundle(
    background_tasks: BackgroundTasks, request: Request
) -> BundleResponse:
    """Generate a diagnostic bundle tar.gz and return its location."""
    config = request.app.state.config
    storage_root = Path(config.storage.root).expanduser()
    bundle_dir = _bundle_dir(storage_root)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    filename = f"helios-bundle-{int(time.time())}-{uuid.uuid4().hex[:8]}.tar.gz"
    bundle_path = bundle_dir / filename

    try:
        size = await _build_bundle_async(request, bundle_path)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "bundle_build_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        # Best-effort cleanup of any partial file.
        try:
            if bundle_path.exists():
                bundle_path.unlink()
        except OSError:
            pass
        raise _err(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "bundle_failed",
            f"failed to build diagnostic bundle: {exc}",
        )

    # Schedule an opportunistic cleanup of old bundles in the same
    # background task pass.
    background_tasks.add_task(_cleanup_old_bundles, bundle_dir, _BUNDLE_TTL_SECONDS)

    log.info("bundle_created", filename=filename, size_bytes=size)
    return BundleResponse(
        bundle_path=str(bundle_path),
        filename=filename,
        download_url=f"/v1/diagnostics/bundle/{filename}",
        size_bytes=size,
        expires_at=time.time() + _BUNDLE_TTL_SECONDS,
    )


@router.get("/diagnostics/bundle/{filename}")
async def download_diagnostic_bundle(
    request: Request,
    filename: str = FsPath(..., min_length=1),
):
    """Stream a previously-built bundle file as a download.

    Hardened against path traversal: only filenames matching our naming
    convention (no slashes, must live directly under the bundle dir)
    are accepted.
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "invalid_filename",
            "filename must not contain path separators",
        )
    config = request.app.state.config
    storage_root = Path(config.storage.root).expanduser()
    bundle_path = _bundle_dir(storage_root) / filename
    if not bundle_path.exists() or not bundle_path.is_file():
        raise _err(
            status.HTTP_404_NOT_FOUND,
            "bundle_not_found",
            f"bundle {filename!r} not found (may have expired)",
        )
    return FileResponse(
        path=str(bundle_path),
        media_type="application/gzip",
        filename=filename,
    )
