"""Tests for the menu bar launchctl helpers (Track 4E.1).

Every public function in :mod:`helios.menubar.launchctl` shells out to
``launchctl``, so the tests monkeypatch ``subprocess.run`` and assert
two things at once:

1. The exact ``argv`` we hand to launchctl matches the install script's
   convention (``gui/<uid>/com.aegis.helios`` service target,
   ``~/Library/LaunchAgents/com.aegis.helios.plist`` plist).
2. The return tuple correctly maps ``(success, output)`` from the
   subprocess result, including edge cases like ``TimeoutExpired`` and
   a missing ``launchctl`` binary.

Tests run on any platform — nothing actually invokes launchctl.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from helios.menubar import launchctl


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeCompleted:
    """Mimics :class:`subprocess.CompletedProcess` for the bits we read."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _expected_service_target() -> str:
    return f"gui/{os.getuid()}/com.aegis.helios"


def _expected_domain_target() -> str:
    return f"gui/{os.getuid()}"


def _patch_run(monkeypatch: pytest.MonkeyPatch, factory):
    """Replace ``subprocess.run`` (the symbol the module imported) with
    ``factory``, which receives the ``argv`` list and returns a
    ``_FakeCompleted`` (or raises).

    Captures every call's argv into ``calls`` so tests can assert on it.
    """
    calls: list[list[str]] = []

    def fake_run(argv, *args: Any, **kwargs: Any):  # noqa: ANN001
        calls.append(list(argv))
        # Tests pass a plain function; let it raise if it wants to.
        return factory(argv, *args, **kwargs)

    monkeypatch.setattr(launchctl.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_plist_path_constant_resolves_to_user_library() -> None:
    """PLIST_PATH must match the install script's destination exactly."""
    expected = Path(os.path.expanduser("~/Library/LaunchAgents/com.aegis.helios.plist"))
    assert launchctl.PLIST_PATH == expected
    # Tilde must be expanded — caller passes this straight to launchctl.
    assert "~" not in str(launchctl.PLIST_PATH)


def test_daemon_label_constant() -> None:
    assert launchctl.DAEMON_LABEL == "com.aegis.helios"


# ---------------------------------------------------------------------------
# daemon_is_loaded()
# ---------------------------------------------------------------------------


def test_daemon_is_loaded_returns_true_when_print_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(0, stdout="service info..."),
    )

    assert launchctl.daemon_is_loaded() is True
    assert calls == [["launchctl", "print", _expected_service_target()]]


def test_daemon_is_loaded_returns_false_when_print_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(
            113, stderr="Could not find service ..."
        ),
    )

    assert launchctl.daemon_is_loaded() is False
    assert calls == [["launchctl", "print", _expected_service_target()]]


# ---------------------------------------------------------------------------
# load_daemon()
# ---------------------------------------------------------------------------


def test_load_daemon_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(0, stdout="ok"),
    )

    ok, msg = launchctl.load_daemon()

    assert ok is True
    assert msg == "ok"
    assert calls == [
        [
            "launchctl",
            "bootstrap",
            _expected_domain_target(),
            str(launchctl.PLIST_PATH),
        ]
    ]


def test_load_daemon_failure_returns_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(
            5, stderr="Bootstrap failed: 5: Input/output error"
        ),
    )

    ok, msg = launchctl.load_daemon()

    assert ok is False
    assert "Bootstrap failed" in msg
    assert "Input/output error" in msg


# ---------------------------------------------------------------------------
# unload_daemon()
# ---------------------------------------------------------------------------


def test_unload_daemon_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(0),
    )

    ok, _msg = launchctl.unload_daemon()

    assert ok is True
    assert calls == [["launchctl", "bootout", _expected_service_target()]]


def test_unload_daemon_when_not_loaded_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the service isn't loaded, bootout exits non-zero. We surface
    that to the caller (no silent swallowing)."""
    _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(
            3, stderr="Could not find service to remove"
        ),
    )

    ok, msg = launchctl.unload_daemon()

    assert ok is False
    assert "Could not find service" in msg


# ---------------------------------------------------------------------------
# kickstart_daemon()
# ---------------------------------------------------------------------------


def test_kickstart_daemon_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(0, stdout="restarted"),
    )

    ok, msg = launchctl.kickstart_daemon()

    assert ok is True
    assert msg == "restarted"
    # The -k flag is load-bearing: it tells launchd to kill before relaunching.
    assert calls == [
        ["launchctl", "kickstart", "-k", _expected_service_target()]
    ]


def test_kickstart_daemon_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(
        monkeypatch,
        lambda argv, *a, **kw: _FakeCompleted(1, stderr="No such process"),
    )

    ok, msg = launchctl.kickstart_daemon()

    assert ok is False
    assert "No such process" in msg


# ---------------------------------------------------------------------------
# Subprocess error paths
# ---------------------------------------------------------------------------


def test_timeout_returns_timeout_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(argv, *a, **kw):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10.0)

    _patch_run(monkeypatch, raiser)

    ok, msg = launchctl.load_daemon()
    assert ok is False
    assert msg == "timeout"


def test_missing_launchctl_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(argv, *a, **kw):  # noqa: ANN001
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    _patch_run(monkeypatch, raiser)

    ok, msg = launchctl.unload_daemon()
    assert ok is False
    assert msg == "launchctl not found"


def test_run_uses_capture_output_text_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-check the subprocess.run kwargs — these are load-bearing for
    PII safety (capture_output + text=True keep output decoded but
    confined) and for liveness (timeout=10 keeps the menu bar responsive).
    """
    seen_kwargs: dict[str, Any] = {}

    def fake_run(argv, *args: Any, **kwargs: Any):  # noqa: ANN001
        seen_kwargs.update(kwargs)
        return _FakeCompleted(0)

    monkeypatch.setattr(launchctl.subprocess, "run", fake_run)
    launchctl.daemon_is_loaded()

    assert seen_kwargs.get("capture_output") is True
    assert seen_kwargs.get("text") is True
    assert seen_kwargs.get("timeout") == 10.0
