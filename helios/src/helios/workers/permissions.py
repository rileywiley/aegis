"""macOS mic + screen-recording permission checker.

Polls periodically, persists each check to ``permission_checks``, and
transitions the daemon state machine to ``error`` on revocation.

The PyObjC calls live behind a :class:`PermissionsBackend` Protocol so
tests can inject a fake without needing real macOS APIs (or even the
PyObjC framework dependencies) at import time.

Wave 1 / Track 2F. Consumed by:

* Wave 2 scheduler — will subscribe to ``check_now()`` results / start
  the periodic loop.
* Wave 3 API — ``GET /v1/permissions`` reads the latest check via
  ``queries.get_last_permission_check``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import aiosqlite

from helios.clock import Clock
from helios.db import queries
from helios.log import get_logger
from helios.state import ComponentError, DaemonStateMachine

_log = get_logger("workers.permissions")

# AVAuthorizationStatus values for AVMediaTypeAudio.
# 0 = notDetermined, 1 = restricted, 2 = denied, 3 = authorized.
_MIC_AUTH_NOT_DETERMINED = 0
_MIC_AUTH_GRANTED = 3


@dataclass
class PermissionState:
    """Snapshot of the most recent permission check.

    ``checked_at`` is UTC epoch seconds (float), sourced from the injected
    :class:`Clock` for testability.
    """

    mic_granted: bool
    screen_recording_granted: bool
    checked_at: float


class PermissionsBackend(Protocol):
    """OS-level permission queries. Injected for testability."""

    def mic_status(self) -> int:
        """Return ``AVAuthorizationStatus`` for mic (0-3)."""

    def screen_recording_granted(self) -> bool:
        """Return ``CGPreflightScreenCaptureAccess()``."""


class _RealBackend:
    """Production backend using PyObjC.

    Imports happen inside the method bodies so that this module loads
    cleanly on non-macOS dev environments and in tests that inject a fake
    backend.
    """

    def mic_status(self) -> int:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio  # type: ignore[import-not-found]

        status = int(
            AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        )
        # If the user has never been asked, fire the OS prompt now so
        # Helios shows up in System Settings → Microphone. Without this,
        # ``authorizationStatusForMediaType_`` will report NotDetermined
        # forever — the OS only prompts when an app actually tries to
        # request access. The completion handler is best-effort; the
        # next check cycle will pick up the user's response.
        if status == _MIC_AUTH_NOT_DETERMINED:
            try:
                AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                    AVMediaTypeAudio, lambda _granted: None
                )
            except Exception:  # pragma: no cover - defensive
                pass
        return status

    def screen_recording_granted(self) -> bool:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore[import-not-found]

        return bool(CGPreflightScreenCaptureAccess())


class PermissionChecker:
    """Periodic permission checker with revocation detection.

    Each :meth:`check_now` call:

    1. Queries the backend for current mic + screen-recording status.
    2. Persists the result to the ``permission_checks`` table.
    3. If a previously-granted permission has flipped to not-granted,
       transitions the state machine to ``mode="error"`` with a
       ``ComponentError`` per revoked permission.

    The first ``check_now()`` call after construction loads the previous
    last-known state from the DB so that a cold start of the daemon does
    not falsely fire revocation when the user simply hadn't granted the
    permission in a prior session.
    """

    def __init__(
        self,
        clock: Clock,
        db: aiosqlite.Connection,
        state_machine: DaemonStateMachine,
        check_interval_seconds: float,
        backend: PermissionsBackend | None = None,
    ) -> None:
        self._clock = clock
        self._db = db
        self._sm = state_machine
        self._interval = check_interval_seconds
        self._backend = backend if backend is not None else _RealBackend()
        self._task: asyncio.Task | None = None
        self._stopping = False
        # Last-known state, used to detect granted → not-granted transitions.
        # Lazily initialized from the DB on the first check_now() call.
        self._last_mic: bool | None = None
        self._last_screen: bool | None = None
        self._initialized: bool = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def check_now(self) -> PermissionState:
        """Run a single check, persist it, detect revocation, return state."""
        try:
            mic_status = self._backend.mic_status()
            mic_granted = mic_status == _MIC_AUTH_GRANTED
        except Exception as exc:  # noqa: BLE001 — backend may raise anything
            _log.warning("mic_check_failed", error=str(exc))
            mic_granted = False

        try:
            screen_granted = self._backend.screen_recording_granted()
        except Exception as exc:  # noqa: BLE001
            _log.warning("screen_check_failed", error=str(exc))
            screen_granted = False

        ts = self._clock.time()

        # Revocation detection runs BEFORE we persist this check, so that
        # the "last known" lookup on a cold start reads the previous run's
        # row (not the one we're about to insert).
        await self._maybe_handle_revocation(
            mic_granted=mic_granted,
            screen_granted=screen_granted,
            ts=ts,
        )

        await queries.insert_permission_check(
            self._db,
            checked_at=ts,
            mic_granted=mic_granted,
            screen_recording_granted=screen_granted,
        )

        self._last_mic = mic_granted
        self._last_screen = screen_granted
        self._initialized = True

        return PermissionState(
            mic_granted=mic_granted,
            screen_recording_granted=screen_granted,
            checked_at=ts,
        )

    async def start(self) -> None:
        """Start the background periodic loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self.run_periodic(), name="permission-checker"
        )

    async def stop(self) -> None:
        """Stop the background loop. Safe to call multiple times."""
        self._stopping = True
        if self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # Either the cancel propagated or the loop raised before we
            # cancelled — both are acceptable shutdown paths.
            pass

    async def run_periodic(self) -> None:
        """Background loop: ``check_now()`` every ``check_interval_seconds``.

        Iteration failures are logged and swallowed so a transient DB or
        backend hiccup doesn't kill the long-running task.
        """
        try:
            while not self._stopping:
                try:
                    await self.check_now()
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "permission_check_iteration_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                if self._stopping:
                    return
                await self._clock.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _maybe_handle_revocation(
        self,
        mic_granted: bool,
        screen_granted: bool,
        ts: float,
    ) -> None:
        # Initialize last-known from DB on first check (so a cold start
        # doesn't fire revocation if the daemon was previously denied).
        if not self._initialized:
            last = await queries.get_last_permission_check(self._db)
            if last is not None:
                self._last_mic = bool(last.mic_granted)
                self._last_screen = bool(last.screen_recording_granted)
            else:
                # No prior data — current observation IS the baseline; do
                # not fire any revocation events on this first check.
                return

        revoked: dict[str, str] = {}
        if self._last_mic is True and not mic_granted:
            revoked["mic"] = "permission_revoked"
        if self._last_screen is True and not screen_granted:
            revoked["screen_recording"] = "permission_revoked"

        # Restoration: if a permission is now granted AND we previously
        # raised an error for it, clear that ComponentError so the daemon
        # can leave ``mode="error"`` without a process restart. This also
        # covers the cold-start case where the new bundle's TCC identity
        # initially shows as not-granted, the user re-grants, and we
        # want the next check to recover without operator intervention.
        snapshot = self._sm.current()
        restored: list[str] = []
        if mic_granted and "mic" in snapshot.component_errors:
            restored.append("mic")
        if screen_granted and "screen_recording" in snapshot.component_errors:
            restored.append("screen_recording")
        if restored:
            self._sm.clear_component_errors(*restored)
            _log.info("permission_restored", components=restored)
            # If the only reason we were in error was these permissions,
            # transition back to armed. We don't second-guess other error
            # sources — only clear when nothing else is broken.
            still_errored = {
                k: v
                for k, v in snapshot.component_errors.items()
                if k not in restored
            }
            if not still_errored and snapshot.mode == "error":
                await self._sm.transition(mode="armed", last_error=None)

        if not revoked:
            return

        component_errors = {
            comp: ComponentError(component=comp, message=msg, occurred_at=ts)
            for comp, msg in revoked.items()
        }
        await self._sm.transition(
            mode="error",
            component_errors=component_errors,
            last_error=f"permission revoked: {', '.join(revoked)}",
        )
        _log.error("permission_revoked", components=list(revoked.keys()))
