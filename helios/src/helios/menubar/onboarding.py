"""Onboarding window for first-launch setup (Track 4D).

Drives the user through the steps documented in HELIOS.md §15:

    1. Welcome
    2. Microphone permission
    3. Screen Recording permission
    4. Restart Helios (conditional)
    5. Transcription model download
    6. Add Helios to Login Items
    7. Complete

The window is built with PyObjC's AppKit framework directly (NSWindow +
NSView per step) rather than using rumps's built-in window — rumps only
exposes a tiny modal alert which is too ugly for a multi-step flow per
HELIOS.md §15.

PyObjC is imported lazily inside method bodies so this module imports
cleanly on non-macOS hosts (CI, Linux dev boxes). The state persistence
helpers and step-navigation logic are testable independently of AppKit.

State persistence
-----------------

State lives at ``~/.aegis/capture/onboarding_state.json`` (chmod 600).
Shape::

    {
        "current_step": int,
        "mic_granted": bool,
        "screen_granted": bool,
        "model_downloaded": bool,
        "login_items_acknowledged": bool,
        "complete": bool
    }

If the state file exists and ``complete`` is False, the window resumes at
the first incomplete step on the next launch. ``is_onboarding_complete``
returns False when the file is missing so the caller can launch the
window on a fresh install.

PII safety
----------

No user account name, hostname, or system identifier is logged. The
state file is written with mode 0600 to keep its contents off other
local accounts. PyObjC errors are logged with a redacted error string.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from helios.log import get_logger

_log = get_logger("menubar.onboarding")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Step indices. The conditional "restart" step is inserted between
# screen-recording and model-download only when the screen-recording grant
# requires a restart of Helios. The numeric ordering is preserved so that
# ``state["current_step"]`` is meaningful even when restart is skipped.
STEP_WELCOME = 0
STEP_MIC = 1
STEP_SCREEN = 2
STEP_RESTART = 3
STEP_MODEL = 4
STEP_LOGIN_ITEMS = 5
STEP_COMPLETE = 6


# Linear order of steps. "restart" is conditional and skipped when not
# needed; ``_next_incomplete_step`` walks this list in order.
_STEP_ORDER: tuple[int, ...] = (
    STEP_WELCOME,
    STEP_MIC,
    STEP_SCREEN,
    STEP_RESTART,
    STEP_MODEL,
    STEP_LOGIN_ITEMS,
    STEP_COMPLETE,
)


# Deep-link URLs for the System Settings panes the user may need to
# visit. Defined as module-level constants so tests can import and
# assert against them without driving the window.
DEEP_LINK_MIC = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
)
DEEP_LINK_SCREEN = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)
DEEP_LINK_LOGIN_ITEMS = (
    "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
)


# Dashboard URL opened when the user clicks "Open Helios Dashboard" on
# the Complete step. Lives in Aegis on the same host.
DASHBOARD_URL = "http://localhost:8000/helios"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    """Return the on-disk path of the onboarding state file.

    Indirected through ``Path.home()`` so tests can monkeypatch
    ``Path.home`` to point at ``tmp_path`` and exercise the real save/load
    code without polluting the developer's home directory.
    """
    return Path.home() / ".aegis" / "capture" / "onboarding_state.json"


def _default_state() -> dict[str, Any]:
    """The initial state for a brand-new install."""
    return {
        "current_step": STEP_WELCOME,
        "mic_granted": False,
        "screen_granted": False,
        "model_downloaded": False,
        "login_items_acknowledged": False,
        "complete": False,
    }


def load_onboarding_state() -> dict[str, Any]:
    """Read the persisted onboarding state.

    Returns an empty dict when the file doesn't exist (signals a fresh
    install — callers should treat this as "start from the beginning").
    Returns an empty dict on a corrupt JSON file too, with a warning
    logged: a corrupt state shouldn't trap the user in a broken window.
    """
    path = _state_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            _log.warning("onboarding_state_unexpected_shape")
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        _log.warning("onboarding_state_load_failed", error=str(exc))
        return {}


def save_onboarding_state(state: dict[str, Any]) -> None:
    """Write the onboarding state to disk with mode 0600.

    Creates the parent directory if missing. Writes via a temp file +
    atomic rename so a crash mid-write can't leave a half-written JSON
    file behind.
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp, path)


def is_onboarding_complete() -> bool:
    """True when the user has finished all required onboarding steps.

    Returns False if the state file is missing (fresh install) or if
    ``complete`` is False / absent.
    """
    state = load_onboarding_state()
    return bool(state.get("complete", False))


# ---------------------------------------------------------------------------
# Step navigation helpers
# ---------------------------------------------------------------------------


def _step_completed(state: dict[str, Any], step: int) -> bool:
    """Return True if ``step`` is already done given ``state``.

    The "done" check uses the most authoritative signal available:

    * Mic / screen / model / login-items: their explicit boolean flags.
    * Complete: the ``complete`` flag (set only when the user clicks Done).
    * Welcome: implicitly done if any later step has any progress; otherwise
      we fall back to ``current_step > 0``. This handles the case where the
      saved state has ``current_step=0`` but a later flag is set (e.g. an
      out-of-band edit, or a programmatic state mutation that didn't bump
      current_step).
    * Restart: implicitly done once screen is granted AND we're not currently
      sitting on the restart step (no needs_restart pending).
    """
    if step == STEP_WELCOME:
        if state.get("current_step", STEP_WELCOME) > STEP_WELCOME:
            return True
        # If any later step has progress, the user must have passed welcome.
        return (
            bool(state.get("mic_granted", False))
            or bool(state.get("screen_granted", False))
            or bool(state.get("model_downloaded", False))
            or bool(state.get("login_items_acknowledged", False))
            or bool(state.get("complete", False))
        )
    if step == STEP_MIC:
        return bool(state.get("mic_granted", False))
    if step == STEP_SCREEN:
        return bool(state.get("screen_granted", False))
    if step == STEP_RESTART:
        # Restart is conditional. It's "done" once screen is granted AND
        # current_step has moved past it — meaning either the restart
        # actually happened, or the path skipped it because no restart
        # was needed. We don't store a dedicated flag because the step
        # is path-dependent.
        if not state.get("screen_granted", False):
            return False
        return state.get("current_step", STEP_WELCOME) > STEP_RESTART
    if step == STEP_MODEL:
        return bool(state.get("model_downloaded", False))
    if step == STEP_LOGIN_ITEMS:
        return bool(state.get("login_items_acknowledged", False))
    if step == STEP_COMPLETE:
        return bool(state.get("complete", False))
    return False


def _next_incomplete_step(state: dict[str, Any]) -> int:
    """Return the first step in ``_STEP_ORDER`` that isn't complete.

    Used on relaunch to resume mid-flow. If everything is complete, the
    Complete step is returned (so the window shows the "Done" button
    rather than disappearing silently).
    """
    for step in _STEP_ORDER:
        if not _step_completed(state, step):
            return step
    return STEP_COMPLETE


# ---------------------------------------------------------------------------
# Permission probes (lazy AppKit imports)
# ---------------------------------------------------------------------------


def _check_mic_status() -> str:
    """Return one of 'granted', 'denied', 'unknown' for the microphone.

    Wraps ``AVCaptureDevice.authorizationStatusForMediaType_("soun")``.
    Returns 'unknown' on any framework error so the UI can still render.
    """
    try:
        import AVFoundation  # type: ignore[import-not-found]
    except ImportError as exc:
        _log.debug("avfoundation_unavailable", error=str(exc))
        return "unknown"

    try:
        # 0 = NotDetermined, 1 = Restricted, 2 = Denied, 3 = Authorized.
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_("soun")
    except Exception as exc:  # noqa: BLE001 — framework can raise anything
        _log.warning("avcapture_status_failed", error=str(exc))
        return "unknown"
    if status == 3:
        return "granted"
    if status in (1, 2):
        return "denied"
    return "unknown"


def _request_mic_access(callback: Callable[[bool], None] | None = None) -> None:
    """Trigger the macOS microphone-permission prompt.

    The framework call is non-blocking; ``callback(granted: bool)`` fires
    on a background thread. We don't await it — the UI poller picks up
    the new state via ``_check_mic_status`` after the user responds.
    """
    try:
        import AVFoundation  # type: ignore[import-not-found]
    except ImportError as exc:
        _log.warning("avfoundation_unavailable", error=str(exc))
        if callback is not None:
            callback(False)
        return

    def _bridge(granted: bool) -> None:
        if callback is not None:
            try:
                callback(bool(granted))
            except Exception as exc:  # noqa: BLE001 — defensive
                _log.warning("mic_request_callback_failed", error=str(exc))

    try:
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            "soun", _bridge
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("avcapture_request_failed", error=str(exc))
        if callback is not None:
            callback(False)


def _check_screen_status() -> str:
    """Return one of 'granted', 'denied', 'unknown' for screen recording.

    Wraps ``CGPreflightScreenCaptureAccess``. Returns 'unknown' if the
    Quartz framework can't be loaded.
    """
    try:
        import Quartz  # type: ignore[import-not-found]
    except ImportError as exc:
        _log.debug("quartz_unavailable", error=str(exc))
        return "unknown"

    try:
        granted = bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception as exc:  # noqa: BLE001
        _log.warning("cgpreflight_failed", error=str(exc))
        return "unknown"
    return "granted" if granted else "denied"


def _request_screen_access() -> bool:
    """Trigger the screen-recording prompt; return True if already granted.

    Calls ``CGRequestScreenCaptureAccess`` which is the safe entry point
    (it doesn't actually start a capture; it just walks the TCC dialog).
    The first call after a fresh grant typically returns False — macOS
    requires the app to be restarted before the new permission is
    visible. The caller should set ``screen_grant_needs_restart`` based
    on the post-call status.
    """
    try:
        import Quartz  # type: ignore[import-not-found]
    except ImportError as exc:
        _log.warning("quartz_unavailable", error=str(exc))
        return False

    try:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception as exc:  # noqa: BLE001
        _log.warning("cgrequest_failed", error=str(exc))
        return False


def _open_url(url: str) -> None:
    """Open ``url`` with macOS's ``open`` command.

    Used for both deep-links into System Settings and the dashboard
    URL on the Complete step. On non-mac, ``subprocess.run`` will raise
    FileNotFoundError; we catch and log so the window doesn't crash.
    """
    try:
        subprocess.run(["open", url], check=False)
    except FileNotFoundError as exc:
        _log.warning("open_command_unavailable", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        _log.warning("open_url_failed", error=str(exc))


# ---------------------------------------------------------------------------
# AppKit bridge
# ---------------------------------------------------------------------------


def _load_appkit() -> Any:
    """Lazy import of the AppKit PyObjC bridge.

    Raises ImportError on non-macOS or if PyObjC isn't installed. The
    onboarding window can't run without AppKit; the caller catches and
    surfaces a friendly RuntimeError to the user.
    """
    import AppKit  # type: ignore[import-not-found]

    return AppKit


# Per-step layout. Each entry is:
#   (header, body, [(button_label, handler_method_name), ...], status_key)
# ``handler_method_name`` is the string name of an OnboardingWindow
# instance method that the ``_ButtonTarget`` bridge calls (it's looked
# up via ``getattr`` so we don't need attribute references at module
# load time).
# ``status_key`` is the ``_step_widgets[step]`` key the runtime poller
# uses to update the visible "Status: granted/denied" label, or None
# for steps without a status indicator.
_STEP_LAYOUT: dict[int, tuple[str, str, list[tuple[str, str]], str | None]] = {
    STEP_WELCOME: (
        "Welcome to Helios",
        "Helios captures meetings + screen content for Aegis. We'll walk "
        "through a few permissions so it can do its job. Click Continue "
        "to start.",
        [("Continue", "_on_welcome_continue")],
        None,
    ),
    STEP_MIC: (
        "Microphone Access",
        "Helios records audio in meetings to build searchable transcripts. "
        "Click Grant Access and approve the macOS prompt. If the prompt "
        "doesn't appear, open System Settings to grant access manually.",
        [
            ("Open System Settings", "_handle_open_mic_settings"),
            ("Grant Access", "_handle_request_mic"),
            ("Continue", "_handle_mic_polled"),
        ],
        "status",
    ),
    STEP_SCREEN: (
        "Screen Recording Access",
        "Helios captures slide / shared-screen content during meetings. "
        "Click Grant Access; macOS may ask you to restart Helios so the "
        "permission can take effect.",
        [
            ("Open System Settings", "_handle_open_screen_settings"),
            ("Grant Access", "_handle_request_screen"),
        ],
        "status",
    ),
    STEP_RESTART: (
        "Restart Helios",
        "macOS requires Helios to relaunch so screen recording can take "
        "effect. Click Restart Now — your progress is saved.",
        [("Restart Now", "_on_restart")],
        None,
    ),
    STEP_MODEL: (
        "Download Transcription Model",
        "Helios uses a local Whisper model for transcription. The download "
        "is ~600 MB and runs once. Click Start to begin.",
        [("Start", "_handle_start_model_download")],
        None,
    ),
    STEP_LOGIN_ITEMS: (
        "Add Helios to Login Items",
        "For Helios to run automatically at login, open Login Items in "
        "System Settings and add Helios to the list.",
        [
            ("Open System Settings", "_handle_open_login_items_settings"),
            ("I added it", "_on_login_items_acked"),
        ],
        None,
    ),
    STEP_COMPLETE: (
        "All set",
        "Helios is ready. Open the dashboard to start exploring captured "
        "meetings, or click Done to dismiss this window.",
        [
            ("Open Helios Dashboard", "_handle_open_dashboard"),
            ("Done", "_on_done"),
        ],
        None,
    ),
}


def _make_button_target_class(appkit: Any, objc: Any) -> Any:
    """Build (or reuse) the ObjC subclass that bridges button clicks to Python.

    AppKit calls ``[target action:sender]`` on the registered selector.
    PyObjC's :func:`objc.selector` lets us expose a Python callable to
    Cocoa, but we must do so via an NSObject subclass — Cocoa won't
    invoke selectors on raw Python objects.

    We cache the subclass on the AppKit module to avoid re-registering
    on every show().
    """
    cached = getattr(appkit, "_HeliosOnboardingButtonTarget", None)
    if cached is not None:
        return cached

    NSObject = appkit.NSObject

    class HeliosOnboardingButtonTarget(NSObject):  # type: ignore[misc]
        def initWithBridge_(self, bridge):  # noqa: N802 - ObjC selector name
            self = objc.super(HeliosOnboardingButtonTarget, self).init()
            if self is None:
                return None
            self._bridge = bridge
            return self

        def doAction_(self, _sender):  # noqa: N802 - ObjC selector name
            try:
                self._bridge()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "onboarding_button_handler_failed", error=str(exc)
                )

    setattr(appkit, "_HeliosOnboardingButtonTarget", HeliosOnboardingButtonTarget)
    return HeliosOnboardingButtonTarget


class _ButtonTarget:
    """Plain Python target object used by buttons.

    ``OnboardingWindow._populate_step_view`` constructs one of these
    per button, holding a strong reference back to the window plus the
    name of the method to invoke on click. The button's NSButton
    actually targets a PyObjC NSObject subclass (see
    :func:`_make_button_target_class`); we store this Python object on
    that subclass so deallocation order stays sane.

    For environments where AppKit isn't fully available (tests with
    MagicMock'd modules), the ObjC bridge isn't actually invoked but
    constructing this class still works — making the build path
    testable.
    """

    def __init__(self, window: "OnboardingWindow", handler_name: str) -> None:
        self._window = window
        self._handler_name = handler_name
        # Lazy: the ObjC bridge object is built on first ``setTarget_``
        # call. We expose ``_objc_target`` so ``setTarget_`` can find it.
        self._objc_target: Any = None

    def doAction_(self, _sender: Any) -> None:  # noqa: N802 - ObjC name
        """Direct fallback used in tests; production path goes via ObjC."""
        self()

    def __call__(self) -> None:
        handler = getattr(self._window, self._handler_name, None)
        if handler is None:
            _log.warning(
                "onboarding_handler_missing", name=self._handler_name
            )
            return
        try:
            handler()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "onboarding_handler_failed",
                name=self._handler_name,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Model-download integration
# ---------------------------------------------------------------------------


def _load_model_downloader() -> Any:
    """Import the ModelDownloader class (Track 4D.6, sibling agent).

    Done lazily so the onboarding module doesn't fail at import time if
    the sibling agent's module is still under construction. Tests mock
    the import via ``sys.modules`` injection.
    """
    from helios.menubar.model_download import ModelDownloader  # type: ignore[import-not-found]

    return ModelDownloader


# ---------------------------------------------------------------------------
# OnboardingWindow
# ---------------------------------------------------------------------------


class OnboardingWindow:
    """First-launch setup window.

    Driven by the menu bar app on first launch (or whenever
    ``is_onboarding_complete()`` is False). Each step is an NSView; the
    window swaps the active view by toggling ``hidden`` flags on a
    container.

    The class is intentionally tolerant of non-macOS environments at
    import time — only ``show()`` actually loads AppKit. ``__init__``
    just hangs onto the callback and reads persisted state so callers
    can construct the object during startup without an X server / AppKit.

    Parameters
    ----------
    on_complete:
        Callable invoked with no arguments when the user clicks "Done"
        on the Complete step. The menu bar app passes a callback that
        starts the daemon-status poller.
    """

    def __init__(self, on_complete: Callable[[], None]) -> None:
        self._on_complete = on_complete
        # State is loaded eagerly: an interrupted onboarding flow should
        # resume at the right step the moment the window is asked to
        # show itself.
        loaded = load_onboarding_state()
        if not loaded:
            loaded = _default_state()
        self._state: dict[str, Any] = loaded
        self._current_step: int = _next_incomplete_step(self._state)
        # AppKit objects are populated by show().
        self._window: Any = None
        self._step_views: dict[int, Any] = {}
        self._content_view: Any = None
        self._screen_grant_needs_restart: bool = False
        # Model-download instance, populated when the user reaches that
        # step. Held on the instance so the progress callbacks can
        # mutate the same object across user interactions.
        self._model_downloader: Any = None
        # NSTimer that polls mic/screen permission state every 500ms
        # while the user is on the corresponding step. Updates the
        # "Status: …" label and auto-advances on grant. Set by
        # ``_start_status_poll`` and torn down by ``_stop_status_poll``.
        self._status_poll_timer: Any = None
        # AppKit references for the main-thread dispatch helper.
        # Imported lazily inside the helper so this module remains
        # importable on non-macOS test hosts.
        self._foundation: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self) -> None:
        """Construct (or reveal) the NSWindow and display the current step.

        Raises a clean ``RuntimeError`` on non-macOS so the menu bar app
        can fall back to a console message rather than crashing with a
        bare ImportError.
        """
        try:
            appkit = _load_appkit()
        except ImportError as exc:
            raise RuntimeError("AppKit not available — onboarding requires macOS") from exc

        if self._window is None:
            self._window = self._build_window(appkit)
            self._step_views = self._build_step_views(appkit)
            for view in self._step_views.values():
                self._content_view.addSubview_(view)

        self._render_current_step()
        try:
            self._window.makeKeyAndOrderFront_(None)
        except Exception as exc:  # noqa: BLE001
            _log.warning("onboarding_window_show_failed", error=str(exc))

    def close(self) -> None:
        """Close the window. Safe to call before ``show()``."""
        # Always stop the permission poll even if the window was never
        # built — keeps a stray NSTimer from outliving the wizard.
        self._stop_status_poll()
        if self._window is None:
            return
        try:
            self._window.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning("onboarding_window_close_failed", error=str(exc))

    # ------------------------------------------------------------------
    # State / step machinery (testable without AppKit)
    # ------------------------------------------------------------------

    @property
    def current_step(self) -> int:
        """Index of the step currently being shown.

        Read by tests to confirm we resumed at the right place after
        loading partial state.
        """
        return self._current_step

    @property
    def state(self) -> dict[str, Any]:
        """Live dict of the persisted state (mutated in place)."""
        return self._state

    def _persist_state(self) -> None:
        """Write the current state to disk and update ``current_step``."""
        self._state["current_step"] = self._current_step
        try:
            save_onboarding_state(self._state)
        except OSError as exc:  # pragma: no cover - defensive
            _log.warning("onboarding_state_save_failed", error=str(exc))

    def _advance_to(self, step: int) -> None:
        """Move to ``step`` and re-render. Persists state."""
        self._current_step = step
        self._persist_state()
        if self._content_view is not None:
            self._render_current_step()

    def _on_welcome_continue(self) -> None:
        """Welcome → Mic."""
        self._advance_to(STEP_MIC)

    def _on_mic_granted(self) -> None:
        """Mark mic granted; advance to screen recording."""
        self._state["mic_granted"] = True
        self._advance_to(STEP_SCREEN)

    def _on_screen_granted(self, needs_restart: bool) -> None:
        """Mark screen recording granted.

        ``needs_restart`` reflects the post-grant ``CGPreflight`` check —
        macOS sometimes requires the app to relaunch before the new
        capability is visible. When true, we route to the Restart step
        first; otherwise we skip directly to model download.
        """
        self._state["screen_granted"] = True
        self._screen_grant_needs_restart = needs_restart
        if needs_restart:
            self._advance_to(STEP_RESTART)
        else:
            self._advance_to(STEP_MODEL)

    def _on_restart(self) -> None:
        """Persist state, then re-exec the Helios menu bar process.

        ``os.execv`` replaces the process image; if we get past the call
        something went wrong. We advance ``current_step`` to MODEL and
        persist *before* execv so the new process resumes on the
        model-download step rather than re-showing the restart prompt.
        """
        self._current_step = STEP_MODEL
        self._persist_state()
        try:
            os.execv(sys.executable, [sys.executable, "-m", "helios"])
        except OSError as exc:
            _log.warning("onboarding_restart_failed", error=str(exc))

    def _on_model_downloaded(self) -> None:
        """Model finished downloading; advance to login items."""
        self._state["model_downloaded"] = True
        self._advance_to(STEP_LOGIN_ITEMS)

    def _on_login_items_acked(self) -> None:
        """User clicked 'I added it' on login-items step."""
        self._state["login_items_acknowledged"] = True
        self._advance_to(STEP_COMPLETE)

    def _on_done(self) -> None:
        """Final 'Done' click: mark complete, fire callback, close."""
        self._state["complete"] = True
        self._persist_state()
        try:
            self._on_complete()
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning("on_complete_callback_failed", error=str(exc))
        self.close()

    # ------------------------------------------------------------------
    # Model download driver (uses sibling agent's ModelDownloader)
    # ------------------------------------------------------------------

    def _start_model_download(
        self,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> None:
        """Kick off the model download and wire progress to the UI.

        ``progress_callback(pct, message)`` is invoked for each progress
        update parsed by the downloader. On completion the model-step is
        marked done and the window advances. Errors are logged and the
        progress message is updated; the user can retry by clicking the
        button again (which calls this method afresh).

        Tests patch ``_load_model_downloader`` to inject a fake
        downloader so we can drive the callbacks without touching the
        real Hugging Face endpoint.
        """
        try:
            ModelDownloader = _load_model_downloader()
        except ImportError as exc:
            _log.warning("model_downloader_unavailable", error=str(exc))
            self._update_model_progress(0.0, "Model downloader not available")
            if progress_callback is not None:
                progress_callback(0.0, "Model downloader not available")
            return

        def _on_progress(pct: float, message: str) -> None:
            # Dispatch the AppKit writes to the main thread — this
            # callback runs on the ModelDownloader subprocess read-loop
            # thread, and AppKit is not thread-safe. Bind ``pct`` /
            # ``message`` into the lambda's defaults so a deferred run
            # doesn't trip on a freed scope (same root cause as the
            # ``exc`` NameError fix in menubar/app.py).
            self._dispatch_to_main_thread(
                lambda pct=pct, message=message: self._update_model_progress(
                    pct, message
                )
            )
            if progress_callback is not None:
                try:
                    progress_callback(pct, message)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("progress_callback_failed", error=str(exc))

        def _on_done() -> None:
            self._dispatch_to_main_thread(self._on_model_downloaded)

        def _on_error(detail: str) -> None:
            _log.warning("model_download_failed", error=str(detail))
            self._dispatch_to_main_thread(
                lambda d=detail: self._render_model_error(d)
            )
            if progress_callback is not None:
                progress_callback(0.0, f"Download failed: {detail}")

        self._model_downloader = ModelDownloader(
            on_progress=_on_progress,
            on_done=_on_done,
            on_error=_on_error,
        )
        try:
            self._model_downloader.start()
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning("model_download_start_failed", error=str(exc))
            self._update_model_progress(0.0, "Failed to start download")
            if progress_callback is not None:
                progress_callback(0.0, "Failed to start download")

    # ------------------------------------------------------------------
    # AppKit construction (untested; covered by manual smoke)
    # ------------------------------------------------------------------

    def _build_window(self, appkit: Any) -> Any:  # pragma: no cover - UI code
        """Construct the NSWindow + content view.

        Style mask is titled+closable; resizable disabled to keep the
        layout simple. The window is centered on the screen and uses a
        standard fixed size suitable for one step at a time.
        """
        # NSWindowStyleMaskTitled (1) | NSWindowStyleMaskClosable (2)
        style_mask = 1 | 2
        rect = appkit.NSMakeRect(0, 0, 520, 380)
        backing_buffered = 2  # NSBackingStoreBuffered
        window = appkit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style_mask, backing_buffered, False
        )
        window.setTitle_("Welcome to Helios")
        window.center()
        # The content view is a plain NSView; each step's view is added
        # as a subview and toggled via setHidden_.
        content = appkit.NSView.alloc().initWithFrame_(rect)
        window.setContentView_(content)
        self._content_view = content
        return window

    def _build_step_views(self, appkit: Any) -> dict[int, Any]:
        """Construct one NSView per step with real widgets.

        Each view is sized to fill the content area. NSButton targets
        are bridged back into Python via the inline ``_ButtonTarget``
        helper class which uses PyObjC's standard NSObject subclass
        registration to receive ``-doAction:`` selector messages.

        Per HELIOS_BUILD_PLAN §4D.2-4D.8 we keep the layout simple:
        a header label, optional status indicator, body text, and one
        or two buttons per step. No auto-layout; absolute frames are
        plenty for a 520x380 wizard.
        """
        rect = appkit.NSMakeRect(0, 0, 520, 380)
        # ``_button_targets`` keeps strong refs to bridge objects so
        # PyObjC doesn't garbage-collect them while AppKit holds weak
        # refs. Without this list, clicking a button would crash.
        if not hasattr(self, "_button_targets"):
            self._button_targets: list[Any] = []  # type: ignore[attr-defined]
        # ``_step_widgets`` exposes individual widgets to the runtime
        # status pollers (mic / screen status indicators get updated
        # text as the user grants permission).
        if not hasattr(self, "_step_widgets"):
            self._step_widgets: dict[int, dict[str, Any]] = {}  # type: ignore[attr-defined]

        views: dict[int, Any] = {}
        for step in _STEP_ORDER:
            view = appkit.NSView.alloc().initWithFrame_(rect)
            self._step_widgets[step] = {}
            try:
                self._populate_step_view(appkit, step, view)
            except Exception as exc:  # noqa: BLE001 - keep building other steps
                _log.warning(
                    "step_view_build_failed", step=step, error=str(exc)
                )
            views[step] = view
        return views

    def _populate_step_view(
        self, appkit: Any, step: int, view: Any
    ) -> None:
        """Attach labels + buttons for a single step.

        Layout convention: header at top, body in middle, buttons in
        bottom row. We use NSTextField for labels and NSButton for
        actions; both are wired to ``self._on_*`` methods.
        """
        header_text, body_text, buttons, status_key = _STEP_LAYOUT[step]
        # Header (28pt bold-ish via NSFont.boldSystemFontOfSize_).
        header = self._make_label(
            appkit,
            header_text,
            frame=appkit.NSMakeRect(20, 320, 480, 30),
            font_size=20,
            bold=True,
        )
        view.addSubview_(header)
        # Body (multi-line, regular).
        body = self._make_label(
            appkit,
            body_text,
            frame=appkit.NSMakeRect(20, 180, 480, 130),
            font_size=13,
            bold=False,
        )
        body.setSelectable_(False) if hasattr(body, "setSelectable_") else None
        view.addSubview_(body)
        if status_key is not None:
            status = self._make_label(
                appkit,
                "Status: checking...",
                frame=appkit.NSMakeRect(20, 145, 480, 22),
                font_size=12,
                bold=False,
            )
            view.addSubview_(status)
            self._step_widgets[step][status_key] = status

        # Buttons sit on the bottom row, right-aligned.
        x = 500
        for label, handler_name in reversed(buttons):
            btn_w = max(120, len(label) * 9 + 24)
            x -= btn_w + 10
            btn = appkit.NSButton.alloc().initWithFrame_(
                appkit.NSMakeRect(x, 20, btn_w, 32)
            )
            btn.setTitle_(label)
            try:
                btn.setBezelStyle_(1)  # NSBezelStyleRounded
            except Exception:  # noqa: BLE001 - older AppKit
                pass
            target = _ButtonTarget(self, handler_name)
            self._button_targets.append(target)  # keep alive

            # Bridge: try the real PyObjC NSObject subclass first.
            # If that fails (e.g. AppKit modules are MagicMocks in
            # tests), fall back to ``setTarget_(target)`` directly so
            # the test build path still produces a button.
            objc_target = self._build_objc_button_target(appkit, target)
            if objc_target is not None:
                btn.setTarget_(objc_target)
                btn.setAction_("doAction:")
                target._objc_target = objc_target  # keep alive
            else:
                try:
                    btn.setTarget_(target)
                    btn.setAction_("doAction:")
                except Exception:  # noqa: BLE001
                    pass
            view.addSubview_(btn)
            self._step_widgets[step].setdefault("buttons", []).append(btn)

        # Model step also gets a progress bar + status label so the user
        # sees feedback while ``download_whisper`` runs as a subprocess.
        # These widgets used to live inside ``_build_objc_button_target``
        # after its ``return None`` — i.e. unreachable dead code — and
        # never made it onto the view, which is why clicking Start
        # produced no visible activity.
        if step == STEP_MODEL:
            try:
                progress = appkit.NSProgressIndicator.alloc().initWithFrame_(
                    appkit.NSMakeRect(20, 110, 480, 20)
                )
                progress.setIndeterminate_(False)
                progress.setMinValue_(0.0)
                progress.setMaxValue_(1.0)
                progress.setDoubleValue_(0.0)
                view.addSubview_(progress)
                self._step_widgets[step]["progress"] = progress
                progress_label = self._make_label(
                    appkit,
                    "",
                    frame=appkit.NSMakeRect(20, 80, 480, 22),
                    font_size=12,
                    bold=False,
                )
                view.addSubview_(progress_label)
                self._step_widgets[step]["progress_label"] = progress_label
            except Exception as exc:  # noqa: BLE001
                _log.warning("model_progress_build_failed", error=str(exc))

    def _build_objc_button_target(
        self, appkit: Any, target: "_ButtonTarget"
    ) -> Any:
        """Return an NSObject-backed bridge for ``target``.

        On real macOS this returns an instance of an NSObject subclass
        whose ``-doAction:`` selector calls ``target()``. In the test
        environment AppKit is a MagicMock and ``objc`` may not be
        importable; we return ``None`` and the caller falls back to a
        plain-Python target.
        """
        try:
            import objc  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            cls = _make_button_target_class(appkit, objc)
            instance = cls.alloc().initWithBridge_(target)
            return instance
        except Exception as exc:  # noqa: BLE001 - test stubs may not support this
            _log.debug("objc_button_target_unavailable", error=str(exc))
            return None

    @staticmethod
    def _make_label(
        appkit: Any,
        text: str,
        *,
        frame: Any,
        font_size: int,
        bold: bool,
    ) -> Any:
        """Build a non-editable NSTextField that behaves like a label."""
        label = appkit.NSTextField.alloc().initWithFrame_(frame)
        label.setStringValue_(text)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        try:
            if bold:
                label.setFont_(appkit.NSFont.boldSystemFontOfSize_(font_size))
            else:
                label.setFont_(appkit.NSFont.systemFontOfSize_(font_size))
        except Exception:  # noqa: BLE001 - font unavailable in test stubs
            pass
        return label

    # The handlers below are tiny method dispatch points the button
    # bridge calls. Keep them as zero-arg wrappers around the existing
    # ``_on_*`` methods (and the deep-link helpers) so the wiring is
    # mechanical and easy to audit.

    def _handle_request_mic(self) -> None:
        _request_mic_access(callback=lambda granted: self._handle_mic_polled())

    def _handle_mic_polled(self) -> None:
        if _check_mic_status() == "granted":
            self._on_mic_granted()

    def _handle_open_mic_settings(self) -> None:
        _open_url(DEEP_LINK_MIC)

    def _handle_request_screen(self) -> None:
        already = _request_screen_access()
        # First call after grant typically returns False — see method docs.
        self._screen_grant_needs_restart = not already
        if _check_screen_status() == "granted":
            self._on_screen_granted(self._screen_grant_needs_restart)

    def _handle_open_screen_settings(self) -> None:
        _open_url(DEEP_LINK_SCREEN)

    def _handle_open_login_items_settings(self) -> None:
        _open_url(DEEP_LINK_LOGIN_ITEMS)

    def _handle_open_dashboard(self) -> None:
        _open_url(DASHBOARD_URL)

    # ------------------------------------------------------------------
    # Main-thread dispatch + permission poller
    # ------------------------------------------------------------------

    def _dispatch_to_main_thread(self, callable_: Any) -> None:
        """Run ``callable_`` on the AppKit main thread.

        ``ModelDownloader`` invokes its on_progress / on_done / on_error
        callbacks from the subprocess-read-loop background thread. AppKit
        is not thread-safe — writing to NSTextField / NSProgressIndicator
        from a worker thread races the UI runloop and the widget update
        silently no-ops. Mirrors ``menubar/app.py._dispatch_to_main_thread``.
        """
        try:
            from Foundation import NSThread, NSOperationQueue  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            _log.debug("main_thread_dispatch_unavailable", error=str(exc))
            try:
                callable_()
            except Exception:  # pragma: no cover
                pass
            return
        try:
            if NSThread.isMainThread():
                callable_()
                return
            NSOperationQueue.mainQueue().addOperationWithBlock_(callable_)
        except Exception as exc:  # noqa: BLE001
            _log.warning("main_thread_dispatch_failed", error=str(exc))
            try:
                callable_()
            except Exception:  # pragma: no cover
                pass

    def _start_status_poll(self, step: int) -> None:
        """Start polling mic/screen status every 500ms for the given step.

        Reads ``_check_mic_status`` / ``_check_screen_status``, updates the
        step's "Status: …" label, and auto-advances on a granted result.
        Idempotent: invalidates any in-flight timer first.

        Uses ``scheduledTimerWithTimeInterval_repeats_block_`` (macOS 10.12+)
        so PyObjC bridges the Python tick into a CFRunLoop block — same
        pattern the voice-note save window uses to keep its auto-save
        countdown working (cycle-2 fix). The plain
        ``…target_selector_userInfo_repeats_`` API needs an NSObject
        target; a plain Python object silently no-ops every fire.
        """
        self._stop_status_poll()
        if step not in (STEP_MIC, STEP_SCREEN):
            return
        try:
            import AppKit  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            _log.debug("status_poll_unavailable", error=str(exc))
            return

        def _tick(_timer: Any) -> None:
            try:
                if step == STEP_MIC:
                    self._handle_mic_poll_tick()
                else:
                    self._handle_screen_poll_tick()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "status_poll_tick_failed", step=step, error=str(exc)
                )

        try:
            self._status_poll_timer = (
                AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                    0.5, True, _tick
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("status_poll_start_failed", error=str(exc))
            self._status_poll_timer = None

    def _stop_status_poll(self) -> None:
        """Invalidate the permission poll timer. Idempotent."""
        if self._status_poll_timer is None:
            return
        try:
            self._status_poll_timer.invalidate()
        except Exception:  # noqa: BLE001 - defensive
            pass
        self._status_poll_timer = None

    def _set_status_label(self, step: int, text: str) -> None:
        """Update ``_step_widgets[step]["status"]`` if present."""
        widgets = getattr(self, "_step_widgets", {}).get(step) or {}
        label = widgets.get("status")
        if label is None:
            return
        try:
            label.setStringValue_(text)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _format_status(value: str, *, granted_text: str = "✓ Granted") -> str:
        """Map ``_check_*_status`` result to a user-readable label."""
        if value == "granted":
            return f"Status: {granted_text}"
        if value == "denied":
            return "Status: Denied — open System Settings to allow"
        if value == "not_determined":
            return "Status: Not requested — click Grant Access"
        if value == "restricted":
            return "Status: Restricted by policy"
        return "Status: checking..."

    def _handle_mic_poll_tick(self) -> None:
        """One periodic check on the mic step. Updates label, auto-advances."""
        value = _check_mic_status()
        self._set_status_label(STEP_MIC, self._format_status(value))
        if value == "granted":
            self._stop_status_poll()
            self._on_mic_granted()

    def _handle_screen_poll_tick(self) -> None:
        """One periodic check on the screen step. Updates label, auto-advances."""
        value = _check_screen_status()
        self._set_status_label(STEP_SCREEN, self._format_status(value))
        if value == "granted":
            self._stop_status_poll()
            # The user reached "granted" by going through System Settings
            # ourselves — no restart prompt is appropriate here.
            self._on_screen_granted(needs_restart=False)

    # ------------------------------------------------------------------
    # Model-download UI helpers
    # ------------------------------------------------------------------

    def _update_model_progress(self, pct: float, message: str) -> None:
        """Drive the progress bar + status label on the model step."""
        widgets = getattr(self, "_step_widgets", {}).get(STEP_MODEL) or {}
        bar = widgets.get("progress")
        if bar is not None:
            try:
                bar.setDoubleValue_(max(0.0, min(1.0, pct)))
            except Exception:  # noqa: BLE001
                pass
        label = widgets.get("progress_label")
        if label is not None and message:
            try:
                label.setStringValue_(message)
            except Exception:  # noqa: BLE001
                pass

    def _render_model_error(self, detail: str) -> None:
        """Show a short error message on the model step's status label."""
        self._update_model_progress(0.0, f"Download failed: {detail}")

    def _handle_start_model_download(self) -> None:
        # Immediate visible feedback so the user knows the click took.
        # ``_start_model_download`` now wires its own main-thread-safe
        # progress / done / error callbacks, so we don't need to pass
        # an external ``progress_callback`` anymore — the widgets are
        # updated directly from ``_update_model_progress``.
        self._update_model_progress(0.0, "Starting download...")
        self._start_model_download(progress_callback=None)

    def _render_current_step(self) -> None:  # pragma: no cover - UI code
        """Hide all step views except the current one.

        Called every time we advance. AppKit's setHidden_ does the
        right thing on already-hidden views, so we don't need to track
        which view was previously visible.

        Also rewires the permission poll timer so the mic / screen
        steps auto-detect grants without a button click.
        """
        for step, view in self._step_views.items():
            try:
                view.setHidden_(step != self._current_step)
            except Exception as exc:  # noqa: BLE001
                _log.warning("step_view_render_failed", error=str(exc))
        self._start_status_poll(self._current_step)


__all__ = [
    "OnboardingWindow",
    "load_onboarding_state",
    "save_onboarding_state",
    "is_onboarding_complete",
    "DEEP_LINK_MIC",
    "DEEP_LINK_SCREEN",
    "DEEP_LINK_LOGIN_ITEMS",
    "DASHBOARD_URL",
    "STEP_WELCOME",
    "STEP_MIC",
    "STEP_SCREEN",
    "STEP_RESTART",
    "STEP_MODEL",
    "STEP_LOGIN_ITEMS",
    "STEP_COMPLETE",
]
