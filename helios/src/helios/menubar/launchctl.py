"""launchctl helpers for the Helios menu bar (Track 4E.1).

Wraps the three ``launchctl`` invocations the menu bar needs to drive
the Helios capture daemon's lifecycle from the user-visible menu:

* :func:`load_daemon`       — ``launchctl bootstrap gui/<uid> <plist>``
* :func:`unload_daemon`     — ``launchctl bootout    gui/<uid>/<label>``
* :func:`kickstart_daemon`  — ``launchctl kickstart -k gui/<uid>/<label>``
* :func:`daemon_is_loaded`  — ``launchctl print      gui/<uid>/<label>``

The exact invocations mirror :file:`scripts/install_helios.sh` so the
runtime helpers and the install script stay byte-compatible (same
service target, same plist path).

Every command-issuing function returns a ``(success, message)`` tuple so
the menu bar can surface the failure reason in an NSAlert without the
caller having to construct error strings itself. ``message`` is a
PII-safe summary — it contains stdout + stderr from ``launchctl`` (which
never includes user content), the literal string ``"timeout"`` on a
``subprocess.TimeoutExpired``, or ``"launchctl not found"`` when the
binary is missing (extremely unlikely on macOS, but plausible if the
test runs on Linux CI).

The module is sync on purpose — rumps' run loop is sync, and every
``launchctl`` call returns within milliseconds in practice. We still
clamp every subprocess call at a 10s timeout so a wedged ``launchctl``
can't freeze the menu bar.

Importing the module on a non-macOS platform is safe: nothing is invoked
at import time, the ``launchctl`` binary is only spawned when one of
the helpers is called.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from helios.log import get_logger

_log = get_logger("menubar.launchctl")

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Launch agent label (matches scripts/install_helios.sh DAEMON_LABEL).
DAEMON_LABEL: str = "com.aegis.helios"

#: Path the install script writes the LaunchAgent plist to.
PLIST_PATH: Path = Path("~/Library/LaunchAgents/com.aegis.helios.plist").expanduser()

#: How long we let ``launchctl`` run before giving up. The real commands
#: are essentially instantaneous, but a wedged launchd would otherwise
#: block the menu bar's run loop indefinitely.
_TIMEOUT_SECONDS: float = 10.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _service_target() -> str:
    """Return the ``gui/<uid>/<label>`` service target launchctl expects."""
    return f"gui/{os.getuid()}/{DAEMON_LABEL}"


def _domain_target() -> str:
    """Return the ``gui/<uid>`` domain target used by ``bootstrap``."""
    return f"gui/{os.getuid()}"


def _run(argv: list[str]) -> tuple[bool, str]:
    """Run ``argv`` and return ``(success, combined_output)``.

    Catches :class:`subprocess.TimeoutExpired` and :class:`FileNotFoundError`
    so the caller never has to handle them directly. Logs only the command
    and the exit code (no PII risk — launchctl output is metadata).
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _log.warning("launchctl_timeout", extra={"argv": argv})
        return False, "timeout"
    except FileNotFoundError:
        _log.warning("launchctl_missing", extra={"argv": argv})
        return False, "launchctl not found"

    combined = (result.stdout or "") + (result.stderr or "")
    success = result.returncode == 0
    _log.info(
        "launchctl_ran",
        extra={"argv": argv, "returncode": result.returncode, "ok": success},
    )
    return success, combined.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def daemon_is_loaded() -> bool:
    """Return ``True`` iff the LaunchAgent is currently bootstrapped.

    Implemented via ``launchctl print gui/<uid>/com.aegis.helios``: a zero
    exit code means launchd knows about the service, non-zero means it
    doesn't (either never bootstrapped or already booted out). We don't
    parse the printed plist — exit code is sufficient.
    """
    ok, _ = _run(["launchctl", "print", _service_target()])
    return ok


def load_daemon() -> tuple[bool, str]:
    """``launchctl bootstrap`` the Helios LaunchAgent into the GUI domain.

    Returns ``(True, output)`` on success, ``(False, error_message)``
    otherwise. Caller is responsible for showing the message to the user
    if it cares (e.g., the menu bar surfaces this in an NSAlert).
    """
    return _run(
        ["launchctl", "bootstrap", _domain_target(), str(PLIST_PATH)]
    )


def unload_daemon() -> tuple[bool, str]:
    """``launchctl bootout`` the Helios LaunchAgent.

    Returns ``(True, output)`` on success, ``(False, error_message)``
    otherwise. NOTE: a non-zero exit is *not* swallowed — if launchctl
    reports the service isn't bootstrapped, the caller decides whether
    "already unloaded" is acceptable. This keeps the helper honest and
    lets the menu bar tell the user something useful.
    """
    return _run(["launchctl", "bootout", _service_target()])


def kickstart_daemon() -> tuple[bool, str]:
    """``launchctl kickstart -k`` the Helios daemon (kill + restart).

    The ``-k`` flag tells launchd to first SIGTERM the running instance
    before relaunching, which is what the menu bar's "Restart Daemon"
    action wants. Returns ``(True, output)`` on success,
    ``(False, error_message)`` otherwise.
    """
    return _run(["launchctl", "kickstart", "-k", _service_target()])
