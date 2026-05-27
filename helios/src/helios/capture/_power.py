"""macOS idle-sleep power assertion via caffeinate(8).

Per HELIOS.md §8.4 / Track 1G: while a capture session is active, prevent
the system from going to idle sleep. Display sleep is allowed.

Spec calls for ``IOPMAssertionCreateWithName`` via PyObjC. We use Apple's
``caffeinate(8)`` instead — it acquires the same
``kIOPMAssertionTypePreventUserIdleSystemSleep`` assertion under the hood
and adds the ``-w PID`` self-cleanup flag, which means the assertion is
auto-released if the daemon crashes (no leaked assertion). The trade-off
is one cheap subprocess per active session.

If ``/usr/bin/caffeinate`` is unavailable (non-Apple OS, sandboxed env,
test runners), `acquire()` returns False and capture proceeds without
the assertion — the system may idle-sleep, but capture itself is not
blocked. Use ``pmset -g assertions`` to confirm the assertion is held
while a session is active.
"""

from __future__ import annotations

import asyncio
import os

from helios.log import get_logger

_log = get_logger("power")

_CAFFEINATE = "/usr/bin/caffeinate"


class PowerAssertion:
    """Holds an idle-sleep assertion via the caffeinate(8) subprocess.

    Idempotent: a second ``acquire()`` while held is a no-op; ``release()``
    is safe to call when not held. Best-effort throughout — failures are
    logged but never raised, so capture is never blocked by a missing
    assertion.
    """

    def __init__(self, caffeinate_path: str = _CAFFEINATE) -> None:
        self._caffeinate_path = caffeinate_path
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def is_held(self) -> bool:
        """True between a successful ``acquire()`` and the matching ``release()``."""
        return self._proc is not None and self._proc.returncode is None

    async def acquire(self) -> bool:
        """Spawn caffeinate to hold the assertion.

        Returns True if the assertion was acquired (or was already held),
        False if caffeinate is unavailable or failed to spawn. Never raises.
        """
        if self.is_held:
            return True
        if not _is_executable(self._caffeinate_path):
            _log.warning(
                "power_assertion_unavailable", path=self._caffeinate_path
            )
            return False
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._caffeinate_path,
                "-i",  # prevent idle sleep (display sleep still allowed)
                "-w",  # auto-release when watched PID exits
                str(os.getpid()),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            _log.info("power_assertion_acquired", caffeinate_pid=self._proc.pid)
            return True
        except Exception as exc:
            _log.warning("power_assertion_acquire_failed", error=str(exc))
            self._proc = None
            return False

    async def release(self) -> None:
        """Terminate the caffeinate subprocess. No-op if not held."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            _log.info("power_assertion_released")
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("power_assertion_release_failed", error=str(exc))


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)
