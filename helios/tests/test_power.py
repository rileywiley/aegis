"""Tests for the caffeinate-based power assertion (Track 1G)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from helios.capture._power import PowerAssertion


# ---------------------------------------------------------------------------
# Acquire / release with a controllable fake caffeinate
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_caffeinate(tmp_path: Path) -> Path:
    """A tiny script that just blocks on stdin and exits cleanly on SIGTERM.

    Mimics caffeinate's behavior of running until killed.
    """
    script = tmp_path / "fake_caffeinate"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "while True:\n"
        "    time.sleep(60)\n"
    )
    script.chmod(0o755)
    return script


async def test_acquire_spawns_process_and_release_terminates(fake_caffeinate):
    pa = PowerAssertion(caffeinate_path=str(fake_caffeinate))
    assert pa.is_held is False

    ok = await pa.acquire()
    assert ok is True
    assert pa.is_held is True
    assert pa._proc is not None
    pid = pa._proc.pid

    await pa.release()
    assert pa.is_held is False
    assert pa._proc is None

    # Confirm the OS reaped the subprocess.
    import errno
    try:
        os.kill(pid, 0)
        alive = True
    except OSError as e:
        alive = e.errno != errno.ESRCH
    assert not alive, f"fake caffeinate pid {pid} still alive after release"


async def test_acquire_is_idempotent(fake_caffeinate):
    pa = PowerAssertion(caffeinate_path=str(fake_caffeinate))
    await pa.acquire()
    proc1 = pa._proc
    ok = await pa.acquire()
    assert ok is True
    assert pa._proc is proc1, "acquire while held should not spawn a second proc"
    await pa.release()


async def test_release_when_not_held_is_noop():
    pa = PowerAssertion(caffeinate_path="/usr/bin/caffeinate")
    # Never acquired — release should not raise.
    await pa.release()
    assert pa.is_held is False


async def test_acquire_returns_false_when_caffeinate_missing(tmp_path):
    pa = PowerAssertion(caffeinate_path=str(tmp_path / "nonexistent"))
    ok = await pa.acquire()
    assert ok is False
    assert pa.is_held is False


async def test_acquire_returns_false_when_caffeinate_not_executable(tmp_path):
    not_exec = tmp_path / "not_exec"
    not_exec.write_text("#!/bin/sh\necho hi\n")
    not_exec.chmod(0o644)  # not executable
    pa = PowerAssertion(caffeinate_path=str(not_exec))
    ok = await pa.acquire()
    assert ok is False


async def test_release_falls_back_to_kill_on_timeout(tmp_path):
    """If the subprocess ignores SIGTERM, release() must SIGKILL."""
    stubborn = tmp_path / "stubborn"
    stubborn.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"  # ignore SIGTERM
        "while True:\n"
        "    time.sleep(60)\n"
    )
    stubborn.chmod(0o755)
    pa = PowerAssertion(caffeinate_path=str(stubborn))
    await pa.acquire()
    pid = pa._proc.pid
    # Release should fall through to .kill() after the 2s wait_for timeout.
    await pa.release()
    import errno
    try:
        os.kill(pid, 0)
        alive = True
    except OSError as e:
        alive = e.errno != errno.ESRCH
    assert not alive, f"stubborn pid {pid} survived release()"


# ---------------------------------------------------------------------------
# Real caffeinate smoke check (skipped if /usr/bin/caffeinate missing)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS-only")
@pytest.mark.skipif(
    not os.access("/usr/bin/caffeinate", os.X_OK),
    reason="/usr/bin/caffeinate not executable in this environment",
)
async def test_real_caffeinate_acquire_release():
    """The real /usr/bin/caffeinate spawns and releases cleanly."""
    pa = PowerAssertion()
    ok = await pa.acquire()
    assert ok is True
    assert pa.is_held is True
    await pa.release()
    assert pa.is_held is False
