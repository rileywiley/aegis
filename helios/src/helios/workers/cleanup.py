"""Retention cleanup worker — HELIOS.md §17 / Phase 5 (Wave 5C).

Two responsibilities:

1. **Daily cleanup** — once a day at ``retention.cleanup_hour_local`` local
   time, scan ``audio_chunks`` for rows whose ``start_ts`` is older than
   ``retention.raw_audio_days`` days. Trashable rows (``status =
   'transcribed'`` AND ``path IS NOT NULL``) get their WAV moved into
   ``<storage.root>/trash/`` and the DB row updated via
   :func:`helios.db.queries.mark_chunk_archived` so the chunk's ``path``
   becomes ``NULL`` and its terminal status sticks. Untranscribed WAVs
   (``recorded``, ``no_audio``, ``unavailable``, ``transcription_failed``)
   are preserved — the user never loses content that hasn't been
   processed yet.

2. **Trash sweep** — after the daily archive pass, files in
   ``<storage.root>/trash/`` whose mtime is older than
   ``retention.trash_hold_hours`` get permanently deleted.

The worker also exposes :func:`trash_session_audio`, a synchronous helper
used by the ``DELETE /v1/sessions/{session_id}`` endpoint to trash a
single session's WAVs out-of-band (i.e. not on the nightly schedule).

Logging contract: emit ``cleanup_run_completed`` / ``cleanup_run_failed``
structured events. Never log raw paths beyond the basename — the
``_filter_sensitive`` processor in :mod:`helios.log` would catch it
anyway but we're conservative.

Wiring: ``daemon.py`` will instantiate :class:`CleanupWorker` at
startup and ``await worker.start()``. This module deliberately does not
mutate ``daemon.py`` — Agent 5C exports the class, the daemon owner
imports it.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiosqlite

from helios.clock import Clock
from helios.config import HeliosConfig
from helios.db import queries
from helios.log import get_logger

_log = get_logger("workers.cleanup")


# ---------------------------------------------------------------------------
# Pure helpers (used by the worker and the DELETE endpoint)
# ---------------------------------------------------------------------------


def _resolve_storage_root(config: HeliosConfig) -> Path:
    """Expand ``config.storage.root`` into an absolute ``Path``."""
    return Path(config.storage.root).expanduser()


def trash_dir_for(storage_root: Path) -> Path:
    """Return (and create) ``<storage_root>/trash/``."""
    trash = storage_root / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    return trash


def _trash_destination(trash_dir: Path, chunk_id: int, source: Path) -> Path:
    """Pick a unique-by-chunk-id destination inside ``trash_dir``.

    Prefixing with ``{chunk_id}_`` guarantees uniqueness (chunk ids are
    PKs) and keeps the original filename visible for debugging /
    manual recovery.
    """
    return trash_dir / f"{chunk_id}_{source.name}"


def _move_to_trash(src: Path, dst: Path) -> bool:
    """Move ``src`` to ``dst``, surviving cross-device renames.

    Returns ``True`` on success, ``False`` when ``src`` does not exist
    (already moved / cleaned). Other errors propagate so the caller
    can decide whether to retry on the next cleanup cycle.
    """
    if not src.exists():
        return False
    # ``Path.rename`` fails across filesystems; ``shutil.move`` handles
    # that. The storage root is normally on the user's home volume so
    # in practice this is a fast rename.
    shutil.move(str(src), str(dst))
    return True


async def trash_session_audio(
    db: aiosqlite.Connection,
    session_id: int,
    storage_root: Path,
) -> int:
    """Move every audio chunk WAV for ``session_id`` into ``storage_root/trash/``.

    Used by ``DELETE /v1/sessions/{session_id}`` so the per-meeting
    deletion path goes through the same trash dir as the nightly job
    (and therefore inherits the 24-hour grace window).

    Does NOT delete DB rows — the DELETE endpoint relies on the
    ``capture_sessions`` cascade for that. Returns the count of files
    actually moved to trash (skips rows whose ``path`` is already NULL
    or whose file is gone).
    """
    trash_dir = trash_dir_for(storage_root)
    chunks = await queries.get_audio_chunks_with_path_for_session(
        db, session_id
    )
    moved = 0
    for chunk in chunks:
        if chunk.path is None:
            continue
        src = Path(chunk.path)
        dst = _trash_destination(trash_dir, chunk.id, src)
        try:
            if _move_to_trash(src, dst):
                moved += 1
        except Exception as exc:  # noqa: BLE001 — best-effort per file
            _log.warning(
                "trash_move_failed",
                session_id=session_id,
                chunk_id=chunk.id,
                filename=src.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
    return moved


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class CleanupReport:
    """Outcome of a single nightly cleanup pass.

    Attributes record counts only — no paths, no chunk-level detail —
    so callers can safely structured-log the whole object.
    """

    archived: int = 0  # WAVs moved into trash this run
    skipped_untranscribed: int = 0  # rows preserved because not transcribed
    purged: int = 0  # files permanently deleted from trash
    errors: int = 0  # per-file failures (non-fatal)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class CleanupWorker:
    """Periodic retention cleanup. Construct, ``start()``, ``stop()``.

    The worker spins up one background task that loops forever, doing:

    1. Compute "seconds until the next ``cleanup_hour_local``."
    2. Sleep on the clock for that many seconds.
    3. Run :meth:`run_cleanup`.
    4. Repeat (re-arming the next-fire time so DST changes are handled
       automatically).

    Construction does not touch the filesystem. ``start()`` is idempotent.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: HeliosConfig,
        clock: Clock,
    ) -> None:
        self._db = db
        self._config = config
        self._clock = clock
        self._loop_task: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------------ public

    async def start(self) -> None:
        """Spawn the daily-tick task. Idempotent."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(
            self._daily_tick_loop(), name="cleanup-worker"
        )

    async def stop(self) -> None:
        """Cancel the background task. Safe to call multiple times."""
        self._stopping = True
        task = self._loop_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._loop_task = None

    async def run_cleanup(self) -> CleanupReport:
        """Run one cleanup pass synchronously. Returns a :class:`CleanupReport`.

        Exposed publicly so the daemon can run a startup catch-up pass
        without waiting for the next scheduled tick (the Mac may have
        been asleep at 3 AM).
        """
        try:
            report = await self._archive_old_wavs()
            self._sweep_trash(report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never crash the worker
            _log.error(
                "cleanup_run_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return CleanupReport(errors=1)
        _log.info(
            "cleanup_run_completed",
            archived=report.archived,
            skipped_untranscribed=report.skipped_untranscribed,
            purged=report.purged,
            errors=report.errors,
        )
        return report

    # ------------------------------------------------------------------ internals

    async def _daily_tick_loop(self) -> None:
        """Sleep until the next ``cleanup_hour_local``, then run cleanup."""
        try:
            while not self._stopping:
                delay = self._seconds_until_next_run()
                await self._clock.sleep(delay)
                if self._stopping:
                    return
                await self.run_cleanup()
        except asyncio.CancelledError:
            raise

    def _seconds_until_next_run(self) -> float:
        """Wall-clock seconds until the next ``cleanup_hour_local``.

        Modelled after :meth:`Scheduler._hard_stop_loop` — uses
        ``clock.now_local()`` so DST handling falls out of the
        datetime arithmetic for free.
        """
        now_local = self._clock.now_local()
        hour = int(self._config.retention.cleanup_hour_local)
        next_fire = now_local.replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        if next_fire <= now_local:
            # Already past today's slot — schedule for tomorrow.
            import datetime as _dt

            next_fire = next_fire + _dt.timedelta(days=1)
        delta = (next_fire - now_local).total_seconds()
        # Floor at zero so a fluky clock can't generate a negative
        # sleep — asyncio.sleep(<0) raises in CPython 3.11+.
        return max(0.0, float(delta))

    async def _archive_old_wavs(self) -> CleanupReport:
        """Move trashable WAVs into ``trash/``. Returns partial report."""
        cutoff = self._clock.time() - (
            int(self._config.retention.raw_audio_days) * 86400
        )
        candidates = await queries.get_transcribed_chunks_older_than(
            self._db, cutoff_ts=cutoff
        )
        storage_root = _resolve_storage_root(self._config)
        trash_dir = trash_dir_for(storage_root)

        report = CleanupReport()
        for chunk in candidates:
            if chunk.path is None:
                # Defensive: get_transcribed_chunks_older_than already
                # filters path IS NOT NULL.
                continue
            src = Path(chunk.path)
            dst = _trash_destination(trash_dir, chunk.id, src)
            try:
                moved = _move_to_trash(src, dst)
            except Exception as exc:  # noqa: BLE001
                report.errors += 1
                _log.warning(
                    "cleanup_trash_failed",
                    chunk_id=chunk.id,
                    filename=src.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            # Whether the file was present or not, clear the DB row so
            # the next pass doesn't keep racing on a phantom path.
            try:
                await queries.mark_chunk_archived(self._db, chunk.id)
            except Exception as exc:  # noqa: BLE001
                report.errors += 1
                _log.warning(
                    "cleanup_mark_archived_failed",
                    chunk_id=chunk.id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            if moved:
                report.archived += 1
            else:
                # Path was registered but the file was already gone —
                # still counts as cleanup progress, but track separately
                # so a noisy filesystem doesn't inflate "archived".
                _log.info(
                    "cleanup_chunk_path_missing_on_disk",
                    chunk_id=chunk.id,
                    filename=src.name,
                )
        # Untranscribed count is informational — fetch quickly so we
        # can emit it in the completion log without a second SQL pass.
        report.skipped_untranscribed = await self._count_untranscribed_old(
            cutoff
        )
        return report

    async def _count_untranscribed_old(self, cutoff_ts: float) -> int:
        """Count audio_chunks older than cutoff that we intentionally skipped.

        Skipped = not yet ``transcribed`` AND still has a path on disk.
        Surfaces as ``skipped_untranscribed`` in
        ``cleanup_run_completed`` logs so operators see when a backlog
        is building. Read-only, no row mutation.
        """
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM audio_chunks "
            "WHERE start_ts < ? "
            "  AND status != 'transcribed' "
            "  AND path IS NOT NULL",
            (cutoff_ts,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    def _sweep_trash(self, report: CleanupReport) -> None:
        """Permanently delete trash files older than ``trash_hold_hours``."""
        storage_root = _resolve_storage_root(self._config)
        trash_dir = trash_dir_for(storage_root)
        hold_seconds = int(self._config.retention.trash_hold_hours) * 3600
        purge_cutoff = self._clock.time() - hold_seconds
        for entry in _iter_trash_files(trash_dir):
            try:
                if entry.stat().st_mtime < purge_cutoff:
                    entry.unlink()
                    report.purged += 1
            except FileNotFoundError:
                # Vanished between iterdir and stat — fine.
                continue
            except Exception as exc:  # noqa: BLE001 — best-effort per file
                report.errors += 1
                _log.warning(
                    "cleanup_purge_failed",
                    filename=entry.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )


def _iter_trash_files(trash_dir: Path) -> Iterable[Path]:
    """Yield top-level files inside ``trash_dir``. Skips subdirs defensively."""
    if not trash_dir.exists():
        return []
    return [p for p in trash_dir.iterdir() if p.is_file()]


__all__ = [
    "CleanupReport",
    "CleanupWorker",
    "trash_dir_for",
    "trash_session_audio",
]
