"""Tests for the onboarding window and state machinery (Track 4D).

Most assertions cover the state-persistence helpers and the step
machine in ``OnboardingWindow``. The AppKit construction itself is
hard to drive in a headless test, so we verify it raises a clean
``RuntimeError`` when AppKit isn't importable, and let manual smoke
tests cover the actual NSWindow rendering.

The tests must run on any host (including non-macOS CI). Real AppKit
isn't available there, so we monkeypatch ``sys.modules`` to inject a
minimal fake when needed and isolate file I/O via tmp_path.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from helios.menubar import onboarding
from helios.menubar.onboarding import (
    DASHBOARD_URL,
    DEEP_LINK_LOGIN_ITEMS,
    DEEP_LINK_MIC,
    DEEP_LINK_SCREEN,
    STEP_COMPLETE,
    STEP_LOGIN_ITEMS,
    STEP_MIC,
    STEP_MODEL,
    STEP_RESTART,
    STEP_SCREEN,
    STEP_WELCOME,
    OnboardingWindow,
    is_onboarding_complete,
    load_onboarding_state,
    save_onboarding_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to ``tmp_path`` for the test.

    Both the module's _state_path() and any deeper code that relies on
    Path.home() will see the tmp dir, so we never touch the developer's
    real ~/.aegis/capture/onboarding_state.json.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# State persistence helpers
# ---------------------------------------------------------------------------


def test_load_returns_empty_when_missing(fake_home: Path) -> None:
    """No state file → empty dict (signals fresh install)."""
    assert load_onboarding_state() == {}


def test_save_writes_json_with_chmod_600(fake_home: Path) -> None:
    """save_onboarding_state writes JSON to ~/.aegis/capture/ at mode 0600."""
    state = {
        "current_step": 2,
        "mic_granted": True,
        "screen_granted": False,
        "model_downloaded": False,
        "login_items_acknowledged": False,
        "complete": False,
    }
    save_onboarding_state(state)

    target = fake_home / ".aegis" / "capture" / "onboarding_state.json"
    assert target.exists(), "state file should exist after save"

    # Verify content is valid JSON and matches what we wrote.
    written = json.loads(target.read_text())
    assert written == state

    # Verify chmod 600 on the persisted file (no group/world bits).
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_state_round_trip(fake_home: Path) -> None:
    """save → load returns the same dict."""
    state = {
        "current_step": 5,
        "mic_granted": True,
        "screen_granted": True,
        "model_downloaded": True,
        "login_items_acknowledged": True,
        "complete": False,
    }
    save_onboarding_state(state)
    loaded = load_onboarding_state()
    assert loaded == state


def test_is_onboarding_complete_missing_file(fake_home: Path) -> None:
    """No state file → not complete (run onboarding on fresh install)."""
    assert is_onboarding_complete() is False


def test_is_onboarding_complete_false_flag(fake_home: Path) -> None:
    """complete=False → not complete."""
    save_onboarding_state({"complete": False})
    assert is_onboarding_complete() is False


def test_is_onboarding_complete_true_flag(fake_home: Path) -> None:
    """complete=True → complete."""
    save_onboarding_state({"complete": True})
    assert is_onboarding_complete() is True


def test_is_onboarding_complete_handles_corrupt_file(fake_home: Path) -> None:
    """A corrupt state file is treated as 'not complete' (no crash)."""
    target = fake_home / ".aegis" / "capture" / "onboarding_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{this is not json")
    # Should not raise; returns False so the user can re-run onboarding.
    assert is_onboarding_complete() is False


# ---------------------------------------------------------------------------
# Deep-link URL constants
# ---------------------------------------------------------------------------


def test_deep_link_mic_url_correct() -> None:
    """Mic deep-link points at the Privacy_Microphone TCC pane."""
    assert DEEP_LINK_MIC == (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
    )


def test_deep_link_screen_url_correct() -> None:
    """Screen deep-link points at the Privacy_ScreenCapture TCC pane."""
    assert DEEP_LINK_SCREEN == (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    )


def test_deep_link_login_items_url_correct() -> None:
    """Login Items deep-link points at the LoginItems-Settings extension."""
    assert DEEP_LINK_LOGIN_ITEMS == (
        "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
    )


def test_dashboard_url_localhost() -> None:
    """Dashboard URL targets the Aegis web UI on localhost:8000."""
    assert DASHBOARD_URL == "http://localhost:8000/helios"


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def test_complete_stays_false_until_all_acked(fake_home: Path) -> None:
    """Setting one flag to True does NOT auto-complete onboarding.

    The Done button is the only thing that flips ``complete`` true; the
    state machine must not infer completion from individual grants.
    """
    window = OnboardingWindow(on_complete=lambda: None)

    # Simulate granting microphone only.
    window._on_mic_granted()
    assert window.state["mic_granted"] is True
    assert window.state["complete"] is False

    # Grant screen too — still not complete; model and login items
    # haven't been done yet.
    window._on_screen_granted(needs_restart=False)
    assert window.state["screen_granted"] is True
    assert window.state["complete"] is False

    # Mark model + login items done.
    window._on_model_downloaded()
    assert window.state["complete"] is False
    window._on_login_items_acked()
    assert window.state["complete"] is False

    # Finally Done → flips complete true.
    window._on_done()
    assert window.state["complete"] is True


def test_resume_jumps_past_already_done_steps(fake_home: Path) -> None:
    """If state shows mic_granted=True, resume at the screen step.

    Verifies the on-launch resume logic walks past completed steps.
    """
    save_onboarding_state(
        {
            "current_step": STEP_WELCOME,  # stale — load logic should override
            "mic_granted": True,
            "screen_granted": False,
            "model_downloaded": False,
            "login_items_acknowledged": False,
            "complete": False,
        }
    )
    window = OnboardingWindow(on_complete=lambda: None)
    assert window.current_step == STEP_SCREEN


def test_resume_with_no_progress_starts_at_welcome(fake_home: Path) -> None:
    """A brand-new (no-state) window starts at the welcome step."""
    window = OnboardingWindow(on_complete=lambda: None)
    assert window.current_step == STEP_WELCOME


def test_resume_with_screen_granted_jumps_to_model(fake_home: Path) -> None:
    """mic+screen granted, restart already done → resume at model download.

    Models the state immediately after ``_on_restart`` re-exec'd Helios:
    the restart handler bumps ``current_step`` to STEP_MODEL and persists
    before execv, so the new process loads with that value. The resume
    walker should keep us at STEP_MODEL.
    """
    save_onboarding_state(
        {
            "current_step": STEP_MODEL,
            "mic_granted": True,
            "screen_granted": True,
            "model_downloaded": False,
            "login_items_acknowledged": False,
            "complete": False,
        }
    )
    window = OnboardingWindow(on_complete=lambda: None)
    assert window.current_step == STEP_MODEL


def test_on_restart_persists_model_step(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_on_restart persists current_step=MODEL *before* execv runs.

    We monkeypatch os.execv to raise (so it doesn't actually replace the
    process under the test runner), then verify the persisted state
    points at STEP_MODEL. This is the contract that lets the post-restart
    resume jump straight to model download.
    """

    def fake_execv(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("execv blocked in tests")

    monkeypatch.setattr(onboarding.os, "execv", fake_execv)

    window = OnboardingWindow(on_complete=lambda: None)
    window._state["mic_granted"] = True
    window._state["screen_granted"] = True
    window._current_step = STEP_RESTART

    window._on_restart()

    persisted = load_onboarding_state()
    assert persisted["current_step"] == STEP_MODEL


def test_resume_with_login_items_acked_jumps_to_complete(fake_home: Path) -> None:
    """All flags True except complete → resume at the Complete step."""
    save_onboarding_state(
        {
            "current_step": STEP_LOGIN_ITEMS,
            "mic_granted": True,
            "screen_granted": True,
            "model_downloaded": True,
            "login_items_acknowledged": True,
            "complete": False,
        }
    )
    window = OnboardingWindow(on_complete=lambda: None)
    assert window.current_step == STEP_COMPLETE


def test_screen_grant_with_restart_routes_to_restart_step(fake_home: Path) -> None:
    """needs_restart=True → advance to the restart step rather than model."""
    window = OnboardingWindow(on_complete=lambda: None)
    window._state["mic_granted"] = True

    window._on_screen_granted(needs_restart=True)

    assert window.current_step == STEP_RESTART


def test_screen_grant_without_restart_routes_to_model(fake_home: Path) -> None:
    """needs_restart=False → skip restart step; go straight to model."""
    window = OnboardingWindow(on_complete=lambda: None)
    window._state["mic_granted"] = True

    window._on_screen_granted(needs_restart=False)

    assert window.current_step == STEP_MODEL


def test_done_callback_fires_and_state_persisted(fake_home: Path) -> None:
    """on_complete callback fires and complete=True is persisted to disk."""
    callback_calls: list[None] = []

    def cb() -> None:
        callback_calls.append(None)

    window = OnboardingWindow(on_complete=cb)
    window._on_done()

    # Callback fired exactly once.
    assert len(callback_calls) == 1

    # State persisted to disk with complete=True.
    persisted = load_onboarding_state()
    assert persisted["complete"] is True
    assert is_onboarding_complete() is True


def test_done_with_callback_raise_does_not_propagate(fake_home: Path) -> None:
    """A buggy callback shouldn't crash the runloop or block close()."""

    def cb() -> None:
        raise RuntimeError("buggy caller")

    window = OnboardingWindow(on_complete=cb)
    # Should not raise.
    window._on_done()
    assert window.state["complete"] is True


# ---------------------------------------------------------------------------
# ModelDownloader integration
# ---------------------------------------------------------------------------


def test_model_downloader_progress_callback_drives_state(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock ModelDownloader; verify on_done advances the window.

    The fake captures the wired callbacks and replays them: progress
    update first, then completion. After completion the window's
    state should show model_downloaded=True and current_step ==
    STEP_LOGIN_ITEMS (the next step after model download).
    """
    captured_callbacks: dict[str, Any] = {}

    class FakeDownloader:
        def __init__(self, on_progress, on_done, on_error) -> None:
            captured_callbacks["progress"] = on_progress
            captured_callbacks["done"] = on_done
            captured_callbacks["error"] = on_error
            captured_callbacks["started"] = False

        def start(self) -> None:
            captured_callbacks["started"] = True

    monkeypatch.setattr(onboarding, "_load_model_downloader", lambda: FakeDownloader)

    window = OnboardingWindow(on_complete=lambda: None)
    # Mark mic + screen granted so the next step after model is login items.
    window._state["mic_granted"] = True
    window._state["screen_granted"] = True

    progress_updates: list[tuple[float, str]] = []
    window._start_model_download(
        progress_callback=lambda pct, msg: progress_updates.append((pct, msg))
    )

    assert captured_callbacks["started"] is True
    assert captured_callbacks["progress"] is not None
    assert captured_callbacks["done"] is not None

    # Replay a progress update; should reach the supplied callback.
    captured_callbacks["progress"](0.42, "downloading…")
    assert progress_updates == [(0.42, "downloading…")]

    # Replay completion; window should advance.
    captured_callbacks["done"]()
    assert window.state["model_downloaded"] is True
    assert window.current_step == STEP_LOGIN_ITEMS


def test_model_downloader_error_does_not_advance(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error from the downloader leaves the user on the model step.

    The user can retry by clicking the button again (which would call
    _start_model_download afresh). We just verify the failure is
    surfaced through the progress callback and state is unchanged.
    """
    captured: dict[str, Any] = {}

    class FakeDownloader:
        def __init__(self, on_progress, on_done, on_error) -> None:
            captured["error"] = on_error

        def start(self) -> None:
            pass

    monkeypatch.setattr(onboarding, "_load_model_downloader", lambda: FakeDownloader)

    window = OnboardingWindow(on_complete=lambda: None)
    window._current_step = STEP_MODEL

    messages: list[tuple[float, str]] = []
    window._start_model_download(progress_callback=lambda p, m: messages.append((p, m)))

    captured["error"]("disk full")

    assert window.state.get("model_downloaded", False) is False
    assert window.current_step == STEP_MODEL
    # Error surfaced to the UI.
    assert any("disk full" in msg for _, msg in messages)


def test_model_downloader_unavailable_logs_and_returns(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ModelDownloader can't be imported, _start_model_download
    surfaces a friendly message via progress_callback without raising.
    """

    def raise_import_error() -> Any:
        raise ImportError("model_download module not yet built")

    monkeypatch.setattr(onboarding, "_load_model_downloader", raise_import_error)

    window = OnboardingWindow(on_complete=lambda: None)
    messages: list[tuple[float, str]] = []
    window._start_model_download(progress_callback=lambda p, m: messages.append((p, m)))

    assert window.state.get("model_downloaded", False) is False
    assert any("not available" in msg.lower() for _, msg in messages)


# ---------------------------------------------------------------------------
# AppKit availability
# ---------------------------------------------------------------------------


def test_show_without_appkit_raises_runtime_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a host without AppKit, show() raises a clean RuntimeError.

    We force the lazy import to fail by inserting None into
    sys.modules — the standard Python sentinel for "do not import this
    module; raise ImportError instead".
    """
    monkeypatch.setitem(sys.modules, "AppKit", None)  # type: ignore[arg-type]

    window = OnboardingWindow(on_complete=lambda: None)
    with pytest.raises(RuntimeError, match="AppKit not available"):
        window.show()


def test_close_before_show_is_noop(fake_home: Path) -> None:
    """close() is safe to call before show() (no AppKit required)."""
    window = OnboardingWindow(on_complete=lambda: None)
    # Should not raise.
    window.close()


def test_module_imports_on_non_mac() -> None:
    """The module itself must import cleanly without AppKit available.

    We don't actually unimport AppKit here — that would require
    re-importing onboarding — but we verify the module is in sys.modules
    after the test fixture has run. The import already happened at the
    top of this test file, which proves the property holds.
    """
    assert "helios.menubar.onboarding" in sys.modules


# ---------------------------------------------------------------------------
# Step views build smoke test
# ---------------------------------------------------------------------------


def test_build_step_views_populates_widgets(fake_home: Path) -> None:
    """``_build_step_views`` should produce non-empty per-step views.

    Each step view should contain at least the header label + at least
    one button. We use a MagicMock AppKit so the test runs on any host.
    """
    appkit = MagicMock(name="AppKit")
    # Make addSubview_ track calls we can inspect later.
    window = OnboardingWindow(on_complete=lambda: None)
    views = window._build_step_views(appkit)
    # Every step in the linear order is present.
    from helios.menubar.onboarding import _STEP_ORDER

    for step in _STEP_ORDER:
        assert step in views

    # Every step should have at least one button registered. The
    # widget map records buttons under each step's "buttons" key.
    for step, widgets in window._step_widgets.items():
        assert "buttons" in widgets, f"step {step} has no buttons"
        assert len(widgets["buttons"]) >= 1, (
            f"step {step} has zero buttons"
        )

    # Total button-target bridges must equal the sum of buttons across
    # all steps — i.e. every button got a target hooked up.
    total_buttons = sum(
        len(w["buttons"]) for w in window._step_widgets.values()
    )
    assert len(window._button_targets) == total_buttons


# ---------------------------------------------------------------------------
# Cycle-3 regressions — permission poller + visible model-download progress
# ---------------------------------------------------------------------------


def test_model_step_has_progress_widget_and_label(fake_home: Path) -> None:
    """Model step view must construct the progress bar + status label.

    Regression: the NSProgressIndicator construction used to live inside
    ``_build_objc_button_target`` after a ``return None`` statement, so
    it was dead code and the model-download step had no visible feedback
    when the user clicked Start.
    """
    appkit = MagicMock(name="AppKit")
    window = OnboardingWindow(on_complete=lambda: None)
    window._build_step_views(appkit)

    widgets = window._step_widgets.get(STEP_MODEL) or {}
    assert "progress" in widgets, "model step missing progress bar widget"
    assert "progress_label" in widgets, "model step missing progress label"
    assert widgets["progress"] is not None
    assert widgets["progress_label"] is not None


def test_update_model_progress_drives_bar_and_label(fake_home: Path) -> None:
    """``_update_model_progress`` writes to both the bar and the label."""
    window = OnboardingWindow(on_complete=lambda: None)
    bar = MagicMock(name="NSProgressIndicator")
    label = MagicMock(name="NSTextField")
    window._step_widgets = {STEP_MODEL: {"progress": bar, "progress_label": label}}

    window._update_model_progress(0.5, "Downloading config.json  50%")

    bar.setDoubleValue_.assert_called_once_with(0.5)
    label.setStringValue_.assert_called_once_with(
        "Downloading config.json  50%"
    )


def test_update_model_progress_clamps_pct(fake_home: Path) -> None:
    """Out-of-range pct values get clamped to [0, 1]."""
    window = OnboardingWindow(on_complete=lambda: None)
    bar = MagicMock(name="NSProgressIndicator")
    window._step_widgets = {STEP_MODEL: {"progress": bar, "progress_label": None}}

    window._update_model_progress(1.5, "")
    bar.setDoubleValue_.assert_called_with(1.0)

    bar.reset_mock()
    window._update_model_progress(-0.2, "")
    bar.setDoubleValue_.assert_called_with(0.0)


def test_format_status_maps_each_backend_value(fake_home: Path) -> None:
    """The status formatter renders every backend result without leaking."""
    fmt = OnboardingWindow._format_status
    assert "Granted" in fmt("granted")
    assert "Denied" in fmt("denied")
    assert "Not requested" in fmt("not_determined")
    assert "Restricted" in fmt("restricted")
    # Unknown values fall back to "checking..." rather than raising.
    assert "checking" in fmt("nonsense")


def test_mic_poll_tick_updates_label_and_advances_on_grant(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``_check_mic_status`` flips to granted, the label updates and
    the wizard advances to the screen step."""
    monkeypatch.setattr(onboarding, "_check_mic_status", lambda: "granted")
    window = OnboardingWindow(on_complete=lambda: None)
    window._current_step = STEP_MIC
    label = MagicMock(name="status_label")
    window._step_widgets = {STEP_MIC: {"status": label}}

    window._handle_mic_poll_tick()

    label.setStringValue_.assert_called_once()
    assert "Granted" in label.setStringValue_.call_args.args[0]
    assert window.current_step == STEP_SCREEN
    assert window.state["mic_granted"] is True


def test_mic_poll_tick_only_updates_label_when_not_granted(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-granted ticks update the label but never advance the step."""
    monkeypatch.setattr(onboarding, "_check_mic_status", lambda: "not_determined")
    window = OnboardingWindow(on_complete=lambda: None)
    window._current_step = STEP_MIC
    label = MagicMock(name="status_label")
    window._step_widgets = {STEP_MIC: {"status": label}}

    window._handle_mic_poll_tick()

    label.setStringValue_.assert_called_once()
    assert "Not requested" in label.setStringValue_.call_args.args[0]
    assert window.current_step == STEP_MIC
    assert window.state.get("mic_granted", False) is False


def test_screen_poll_tick_advances_without_restart_prompt(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Granted ticks for screen auto-advance without showing the
    restart prompt (the user already enabled it in System Settings)."""
    monkeypatch.setattr(onboarding, "_check_screen_status", lambda: "granted")
    window = OnboardingWindow(on_complete=lambda: None)
    window._current_step = STEP_SCREEN
    label = MagicMock(name="status_label")
    window._step_widgets = {STEP_SCREEN: {"status": label}}

    window._handle_screen_poll_tick()

    # Advances directly to the model step (skipping STEP_RESTART).
    assert window.current_step == STEP_MODEL
    assert window.state["screen_granted"] is True


def test_model_downloader_progress_callback_dispatches_to_main_thread(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_on_progress`` from the downloader read-loop thread must hop to
    the main thread before touching AppKit widgets — regression for the
    silent-no-op-on-worker-thread class of bug."""
    dispatched: list[Any] = []

    captured: dict[str, Any] = {}

    class FakeDownloader:
        def __init__(self, on_progress, on_done, on_error) -> None:
            captured["progress"] = on_progress

        def start(self) -> None:  # pragma: no cover - not exercised
            pass

    monkeypatch.setattr(onboarding, "_load_model_downloader", lambda: FakeDownloader)

    window = OnboardingWindow(on_complete=lambda: None)
    monkeypatch.setattr(
        window, "_dispatch_to_main_thread", lambda fn: dispatched.append(fn)
    )
    window._start_model_download(progress_callback=None)

    captured["progress"](0.3, "downloading model.bin  30%")
    assert len(dispatched) == 1, (
        "progress callback must dispatch exactly one main-thread block"
    )
    # The dispatched callable should be safe to invoke and should update
    # widgets via _update_model_progress; just confirm it runs without
    # raising and is a callable (the integration test with real widgets
    # is covered separately).
    assert callable(dispatched[0])


def test_status_poll_stops_when_window_closes(fake_home: Path) -> None:
    """``close()`` must stop the permission poll timer."""
    window = OnboardingWindow(on_complete=lambda: None)
    fake_timer = MagicMock(name="NSTimer")
    window._status_poll_timer = fake_timer

    window.close()

    fake_timer.invalidate.assert_called_once()
    assert window._status_poll_timer is None
