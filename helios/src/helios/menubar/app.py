"""Helios menu bar app — full integration of every Wave 1 module.

Per HELIOS.md §12 the menu bar is the user-facing surface for the
Helios daemon. It runs in a separate process from the daemon (login
session, not a launchd-managed background job) and talks to the
daemon over HTTP on ``127.0.0.1:3031``.

This module is the **only** consumer of the Wave 1 menubar package
modules — the integrator. Wave 1 modules expose narrow public APIs
(client / notifier / hotkey / launchctl / indicator / save-window /
onboarding); this module wires them together into the rumps run-loop.

Architecture (HELIOS.md §12.1)
-------------------------------

* ``rumps.App`` subclass; runloop is sync.
* ``rumps.Timer`` ticks the polling loop every 3s.
* On each tick: ``client.get_status()`` + ``client.get_active_voice_note()``
  drive a 6-state state machine. State change → rebuild menu, switch
  icon. Indicator + save-window lifecycle is also driven from here.
* PyObjC is **lazy-imported** in helper functions (NSAlert, deep-link
  open) so the module is import-clean on non-macOS hosts.
* Voice-note hotkey (Track 4F.3) is async; the menu bar app spawns a
  dedicated asyncio loop in a background thread and bridges via
  ``asyncio.run_coroutine_threadsafe``.
* No raw audio / transcript / titles in logs (PII discipline).

Six icon states (HELIOS.md §12.1):

* ``not_running``           — daemon down (DaemonUnreachable)
* ``armed``                 — daemon up, no active session
* ``recording``             — calendar / continuous capture active
* ``recording_voice_note``  — voice note active (overrides ``recording``)
* ``paused``                — daemon paused until a future timestamp
* ``error``                 — at least one component is in an error state

Tasks integrated:
4B (rumps app), 4C.3-5 (notification callbacks), 4E.2-3 (launchctl
"Stop Helios Daemon" + "Start Capture Daemon"), 4F.3 (hotkey wiring),
4G + 4H (indicator + save-window lifecycle), 4D wiring (onboarding on
first launch).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import rumps

from helios.clock import RealClock
from helios.config import HeliosConfig, get_config, load_config
from helios.log import get_logger
from helios.menubar import launchctl as launchctl_helpers
from helios.menubar.client import (
    DaemonAuthFailed,
    DaemonStatus,
    DaemonTimeout,
    DaemonUnreachable,
    HeliosClient,
)
from helios.menubar.hotkey import VoiceNoteHotkey
from helios.menubar.notifications import MenuBarNotifier
from helios.menubar.onboarding import (
    DEEP_LINK_MIC,
    DEEP_LINK_SCREEN,
    OnboardingWindow,
    is_onboarding_complete,
)
from helios.menubar.voice_note_indicator import VoiceNoteIndicator
from helios.menubar.voice_note_save_window import VoiceNoteSaveWindow

_log = get_logger("menubar.app")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Six icon states per HELIOS.md §12.1.
STATE_NOT_RUNNING = "not_running"
STATE_ARMED = "armed"
STATE_RECORDING = "recording"
STATE_RECORDING_VOICE_NOTE = "recording_voice_note"
STATE_PAUSED = "paused"
STATE_ERROR = "error"


# Polling cadence — 3s per HELIOS.md §12.2.
_POLL_INTERVAL_SECONDS = 3.0


# Aegis dashboard / settings URLs.
_DASHBOARD_URL = "http://localhost:8000/helios"
_DASHBOARD_SETTINGS_URL = "http://localhost:8000/helios/settings"
_AEGIS_BASE_URL = "http://127.0.0.1:8000"


# Daemon URL + token path. Mirrors HELIOS.md §7.1.
_DAEMON_URL = "http://127.0.0.1:3031"
_TOKEN_PATH = Path("~/.aegis/capture.toml").expanduser()


# Deep-link to the macOS Accessibility settings pane (for the "hotkey
# registration failed" flow per HELIOS.md §14.1).
_DEEP_LINK_ACCESSIBILITY = (
    "x-apple.systempreferences:com.apple.preference.security?"
    "Privacy_Accessibility"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _icon_path(state: str) -> str | None:
    """Resolve the icon path for ``state`` in dev or py2app bundle.

    Looks first in ``$RESOURCEPATH/icons/`` (set by py2app), falls back
    to ``helios/icons/`` relative to this source file. Returns ``None``
    when the icon isn't available — rumps falls back to the title.
    """
    name = f"helios_{state}_template.png"
    resource_path = os.environ.get("RESOURCEPATH")
    if resource_path:
        candidate = Path(resource_path) / "icons" / name
    else:
        # Dev: this file lives at helios/src/helios/menubar/app.py;
        # icons live at helios/icons/ (parents[3] = helios/).
        candidate = Path(__file__).resolve().parents[3] / "icons" / name
    return str(candidate) if candidate.exists() else None


def _open_url(url: str) -> None:
    """Open ``url`` via ``open`` on macOS, ``webbrowser`` elsewhere.

    macOS handles ``x-apple.systempreferences:`` deep-links via the
    ``open`` command but ``webbrowser.open`` doesn't always recognize
    custom URL schemes. We try ``open`` first and fall back to
    webbrowser so the menu still works on dev hosts.
    """
    try:
        subprocess.run(["open", url], check=False)
        return
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 — defensive
        _log.warning("open_url_subprocess_failed", error=str(exc))

    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        _log.warning("open_url_webbrowser_failed", error=str(exc))


def _next_morning(now: datetime, morning_hour: int) -> datetime:
    """Return the next occurrence of ``morning_hour:00`` local time.

    If ``now.hour < morning_hour``, returns today at ``morning_hour:00``;
    otherwise returns tomorrow. Preserves tzinfo from ``now``.
    """
    target = now.replace(hour=morning_hour, minute=0, second=0, microsecond=0)
    if now < target:
        return target
    return target + timedelta(days=1)


def _next_monday_morning(now: datetime, morning_hour: int) -> datetime:
    """Return next Monday at ``morning_hour:00`` local time.

    If today is Monday and we're before ``morning_hour``, returns today.
    Otherwise rolls forward to the next Monday.
    """
    days_ahead = (0 - now.weekday()) % 7  # 0 = Monday
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=morning_hour, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate = candidate + timedelta(days=7)
    return candidate


def _meeting_url(calendar_event_id: str) -> str:
    """Build the Aegis meeting deep-link for a calendar event id."""
    # Aegis renders meetings under /meetings/<id>; the calendar_event_id
    # is the canonical key that Aegis indexes by. If the user clicks
    # before Aegis has ingested the event, the page is empty but
    # navigable — better than failing silently.
    return f"{_AEGIS_BASE_URL}/meetings?calendar_event_id={calendar_event_id}"


def _confirm_alert(title: str, message: str, ok: str, cancel: str) -> bool:
    """Show a modal NSAlert via rumps and return True if user clicked OK.

    rumps.alert returns 1 for the OK button, 0 for cancel.
    """
    try:
        result = rumps.alert(
            title=title, message=message, ok=ok, cancel=cancel
        )
        return bool(result == 1)
    except Exception as exc:  # noqa: BLE001 — defensive
        _log.warning("confirm_alert_failed", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def derive_state(
    status: DaemonStatus | None,
    voice_note_active: bool,
) -> str:
    """Map a daemon status + voice-note flag onto one of the six states.

    Public for tests — they call this directly to verify the state
    machine without spinning up a full rumps app.

    ``status=None`` means we couldn't reach the daemon (caller maps a
    DaemonUnreachable to None).
    """
    if status is None:
        return STATE_NOT_RUNNING
    if voice_note_active:
        return STATE_RECORDING_VOICE_NOTE
    mode = status.mode
    if mode == "armed":
        return STATE_ARMED
    if mode == "recording":
        return STATE_RECORDING
    if mode == "paused":
        return STATE_PAUSED
    if mode == "error":
        return STATE_ERROR
    if mode == "not_running":
        return STATE_NOT_RUNNING
    # Defensive default: an unknown mode means the wire schema drifted.
    return STATE_ERROR


# ---------------------------------------------------------------------------
# Pause submenu helpers (Task 4B.3)
# ---------------------------------------------------------------------------


def pause_submenu_options(
    now: datetime, morning_hour: int
) -> list[tuple[str, datetime]]:
    """Return ``(label, target_dt)`` tuples for the pause submenu.

    Public for tests. Mirrors HELIOS.md §12.4:

    * "1 hour" — always shown
    * "Until morning" / "Until tomorrow morning" — always shown
    * "Until Monday morning" — only Fri/Sat/Sun (weekday >= 4)
    """
    options: list[tuple[str, datetime]] = []
    options.append(("1 hour", now + timedelta(hours=1)))

    morning_target = _next_morning(now, morning_hour)
    if now.hour < morning_hour:
        morning_label = "Until morning"
    else:
        morning_label = "Until tomorrow morning"
    options.append((morning_label, morning_target))

    if now.weekday() >= 4:  # Fri=4, Sat=5, Sun=6
        options.append(
            ("Until Monday morning", _next_monday_morning(now, morning_hour))
        )

    return options


# ---------------------------------------------------------------------------
# HeliosApp
# ---------------------------------------------------------------------------


class HeliosApp(rumps.App):
    """rumps menu bar app integrating every Wave 1 module.

    Constructor keeps ``poll_timer`` stopped until ``start_polling()``
    is called so onboarding can defer the polling cycle to its
    completion callback.
    """

    def __init__(
        self,
        client: HeliosClient | None = None,
        notifier: MenuBarNotifier | None = None,
        config: HeliosConfig | None = None,
        clock: Any | None = None,
    ) -> None:
        # rumps.App needs an icon at construction. We start in
        # not_running and let the first poll override.
        super().__init__(
            "Helios",
            icon=_icon_path(STATE_NOT_RUNNING),
            template=True,
            quit_button=None,
        )

        # Inject-or-build the dependencies. Tests pass mocks; production
        # wires real ones.
        self._config: HeliosConfig = config if config is not None else self._safe_load_config()
        self._client: HeliosClient = client if client is not None else HeliosClient(
            base_url=_DAEMON_URL,
            token_path=_TOKEN_PATH,
        )
        self._notifier: MenuBarNotifier = notifier if notifier is not None else MenuBarNotifier()
        self._clock = clock if clock is not None else RealClock()

        # Mutable runtime state observable by tests.
        self._last_status: DaemonStatus | None = None
        self._last_state: str | None = None
        self._voice_note_active: bool = False
        # ``True`` while the user-initiated stop path
        # (``_trigger_voice_note_stop``) is in flight or has just
        # completed. Used by ``_poll`` to distinguish a user stop from
        # a force-stop on the active→null transition: only the
        # force-stop case should kick off a fresh stop+save-window
        # fetch, since the user-stop path already owns that work.
        self._voice_note_stop_handled_by_user: bool = False

        # Lifecycle handles for the indicator + save window.
        self._indicator: VoiceNoteIndicator | None = None
        self._save_window: VoiceNoteSaveWindow | None = None

        # Hotkey + asyncio loop (started lazily via _maybe_start_hotkey).
        self._hotkey: VoiceNoteHotkey | None = None
        self._asyncio_loop: asyncio.AbstractEventLoop | None = None
        self._asyncio_thread: threading.Thread | None = None

        # Timer (started by ``start_polling`` after onboarding completes).
        self._poll_timer = rumps.Timer(self._poll, _POLL_INTERVAL_SECONDS)

        # Register notification categories + wire callbacks.
        self._notifier.register_categories()
        self._wire_notification_callbacks()

        # Build initial menu so the bar item is interactive even before
        # the first poll completes.
        self._rebuild_menu()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start_polling(self) -> None:
        """Begin the 3s status polling loop. Idempotent."""
        if self._poll_timer.is_alive():
            return
        # Run an immediate poll so the menu bar reflects state right
        # away (rather than waiting 3s for the first timer tick).
        self._poll(self._poll_timer)
        try:
            self._poll_timer.start()
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning("poll_timer_start_failed", error=str(exc))

    def stop_polling(self) -> None:
        """Halt the polling loop. Idempotent."""
        try:
            self._poll_timer.stop()
        except Exception as exc:  # noqa: BLE001
            _log.warning("poll_timer_stop_failed", error=str(exc))

    def maybe_show_onboarding(self) -> bool:
        """Show the onboarding window if the user hasn't completed it.

        Returns True when the window was shown (caller defers polling
        until the on_complete callback fires) and False when onboarding
        was already complete (caller starts polling immediately).
        """
        if is_onboarding_complete():
            return False

        try:
            window = OnboardingWindow(on_complete=self.start_polling)
            window.show()
        except RuntimeError as exc:
            # AppKit not available (non-mac dev) — fall through to
            # polling-only mode so the rest of the app is still usable.
            _log.warning("onboarding_unavailable", error=str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            _log.warning("onboarding_show_failed", error=str(exc))
            return False
        return True

    def maybe_start_hotkey(self) -> None:
        """Spawn the voice-note hotkey listener if enabled in config.

        Hotkey is async; we run a dedicated asyncio loop in a daemon
        thread and bridge into it via ``run_coroutine_threadsafe``.
        Idempotent.
        """
        if not self._config.voice_note.hotkey_enabled:
            return
        if self._hotkey is not None:
            return

        combo = self._config.voice_note.hotkey_combo
        try:
            self._hotkey = VoiceNoteHotkey(
                combo=combo,
                on_trigger=self._on_hotkey_trigger,
                clock=self._clock,
            )
        except ValueError as exc:
            # Bad combo string in config — log and leave _hotkey unset.
            _log.warning("hotkey_combo_invalid", error=str(exc), combo=combo)
            self._hotkey = None
            return

        # Spawn asyncio loop in a background thread.
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever,
            name="helios-menubar-asyncio",
            daemon=True,
        )
        thread.start()
        self._asyncio_loop = loop
        self._asyncio_thread = thread

        try:
            asyncio.run_coroutine_threadsafe(self._hotkey.start(), loop)
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning("hotkey_start_dispatch_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll(self, _timer: Any) -> None:
        """One poll cycle. Reads status + active-voice-note, updates UI."""
        status: DaemonStatus | None = None
        try:
            status = self._client.get_status()
        except DaemonUnreachable:
            # Most common case during a daemon restart.
            status = None
        except (DaemonTimeout, DaemonAuthFailed) as exc:
            _log.warning(
                "poll_status_failed",
                error_type=type(exc).__name__,
            )
            status = None
        except Exception as exc:  # noqa: BLE001
            _log.warning("poll_status_unexpected", error=str(exc))
            status = None

        voice_note_active = False
        if status is not None:
            try:
                resp = self._client.get_active_voice_note()
                voice_note_active = resp.active is not None
            except (
                DaemonUnreachable,
                DaemonTimeout,
                DaemonAuthFailed,
            ):
                voice_note_active = False
            except Exception as exc:  # noqa: BLE001
                _log.warning("poll_voice_note_failed", error=str(exc))
                voice_note_active = False

        new_state = derive_state(status, voice_note_active)
        prev_state = self._last_state
        prev_voice_note_active = self._voice_note_active
        self._last_status = status
        self._voice_note_active = voice_note_active

        # Voice-note ended without the user clicking Stop (cap-timer
        # force-stop, external cancel, daemon restart, etc.). Trigger
        # the same stop+save-window worker so the user can still review
        # the transcript: the daemon's /v1/voice-note/stop endpoint
        # accepts post-force-stop calls and returns the buffered
        # transcript built from the chunks the scheduler had already
        # captured.
        if prev_voice_note_active and not voice_note_active:
            if self._voice_note_stop_handled_by_user:
                self._voice_note_stop_handled_by_user = False
            else:
                _log.info("voice_note_ended_externally_dispatching_save_window")
                self._trigger_voice_note_stop()

        # Indicator lifecycle — driven by the voice-note state.
        if new_state == STATE_RECORDING_VOICE_NOTE and self._indicator is None:
            self._spawn_indicator()
        elif (
            new_state != STATE_RECORDING_VOICE_NOTE
            and self._indicator is not None
        ):
            # Indicator self-dismisses on its own poll (active=null), but
            # we drop our reference so a fresh ``_spawn_indicator`` call
            # builds a new one cleanly on the next voice note.
            self._indicator = None

        if new_state != prev_state:
            _log.info(
                "menubar_state_changed",
                old=prev_state,
                new=new_state,
            )
            self._set_icon(new_state)
            self._last_state = new_state
            self._rebuild_menu()

    # ------------------------------------------------------------------
    # Icon switching
    # ------------------------------------------------------------------

    def _set_icon(self, state: str) -> None:
        """Update the menu bar icon to reflect ``state``."""
        path = _icon_path(state)
        if path is None:
            # Missing icon file — don't crash, just log.
            _log.warning("icon_missing", state=state)
            return
        try:
            self.icon = path
        except Exception as exc:  # noqa: BLE001
            _log.warning("icon_set_failed", state=state, error=str(exc))

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def _rebuild_menu(self) -> None:
        """Clear + repopulate the menu based on current state."""
        try:
            self.menu.clear()
        except Exception as exc:  # noqa: BLE001 — defensive (clear can fail
            # mid-construction when the menu is empty in some rumps versions).
            _log.debug("menu_clear_failed", error=str(exc))

        state = self._last_state or STATE_NOT_RUNNING
        status = self._last_status

        # Header.
        try:
            self.menu.add(self._build_header(state, status))
            self.menu.add(rumps.separator)
        except Exception as exc:  # noqa: BLE001
            _log.warning("menu_header_failed", error=str(exc))

        # Primary action.
        try:
            for item in self._primary_action_items(state):
                self.menu.add(item)
            self.menu.add(rumps.separator)
        except Exception as exc:  # noqa: BLE001
            _log.warning("menu_primary_failed", error=str(exc))

        # Voice note item (only when daemon running).
        if state != STATE_NOT_RUNNING:
            try:
                self.menu.add(self._voice_note_item(state))
            except Exception as exc:  # noqa: BLE001
                _log.warning("menu_voice_note_failed", error=str(exc))

        # Capture Screen submenu (always when daemon running).
        if state != STATE_NOT_RUNNING:
            try:
                self.menu.add(self._screen_capture_submenu())
            except Exception as exc:  # noqa: BLE001
                _log.warning("menu_screen_failed", error=str(exc))

        # Pause submenu — only when armed/recording (not paused itself).
        if state in (STATE_ARMED, STATE_RECORDING):
            try:
                self.menu.add(self._pause_submenu())
            except Exception as exc:  # noqa: BLE001
                _log.warning("menu_pause_failed", error=str(exc))

        try:
            self.menu.add(rumps.separator)
            self.menu.add(
                rumps.MenuItem("Open Dashboard", callback=self._on_open_dashboard)
            )
            self.menu.add(
                rumps.MenuItem(
                    "Preferences…", callback=self._on_open_preferences
                )
            )
            self.menu.add(rumps.separator)
            self.menu.add(
                rumps.MenuItem(
                    "Re-run Onboarding", callback=self._on_rerun_onboarding
                )
            )
            self.menu.add(rumps.separator)
            self.menu.add(
                rumps.MenuItem("Quit Menu Bar", callback=self._on_quit_menu_bar)
            )
            self.menu.add(
                rumps.MenuItem(
                    "Stop Helios Daemon…", callback=self._on_stop_daemon
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("menu_footer_failed", error=str(exc))

    def _build_header(
        self, state: str, status: DaemonStatus | None
    ) -> rumps.MenuItem:
        """Top header line — context for the current state.

        Click handler:
            * ``recording`` w/ calendar event → opens Aegis meeting page
            * Other states → inert
        """
        text = self._header_text(state, status)
        callback = None
        if state == STATE_RECORDING and status is not None:
            ids = (
                status.active_session.calendar_event_ids
                if status.active_session is not None
                else []
            )
            if ids:
                event_id = ids[0]
                callback = lambda _item, _eid=event_id: _open_url(
                    _meeting_url(_eid)
                )
        return rumps.MenuItem(text, callback=callback)

    @staticmethod
    def _header_text(state: str, status: DaemonStatus | None) -> str:
        """Produce a one-line header for ``state``."""
        if state == STATE_NOT_RUNNING:
            return "Helios — daemon not running"
        if state == STATE_ARMED:
            return "Helios — armed"
        if state == STATE_RECORDING:
            kind = "capture"
            if status is not None and status.active_session is not None:
                kind = status.active_session.kind
            if kind == "calendar":
                return "Recording — calendar meeting"
            if kind == "continuous":
                return "Recording — continuous"
            if kind == "manual_screen":
                return "Recording — manual screen capture"
            return "Recording"
        if state == STATE_RECORDING_VOICE_NOTE:
            return "Recording voice note"
        if state == STATE_PAUSED:
            return "Helios — paused"
        if state == STATE_ERROR:
            return "Helios — error"
        return "Helios"

    def _primary_action_items(self, state: str) -> list[rumps.MenuItem]:
        """Build the primary-action menu items for ``state``."""
        items: list[rumps.MenuItem] = []
        if state == STATE_NOT_RUNNING:
            items.append(
                rumps.MenuItem(
                    "Start Capture Daemon", callback=self._on_start_daemon
                )
            )
        elif state == STATE_ARMED:
            items.append(
                rumps.MenuItem(
                    "Start Continuous Capture",
                    callback=self._on_start_continuous,
                )
            )
        elif state in (STATE_RECORDING, STATE_RECORDING_VOICE_NOTE):
            items.append(
                rumps.MenuItem("Stop Capture", callback=self._on_stop_capture)
            )
        elif state == STATE_PAUSED:
            items.append(rumps.MenuItem("Resume", callback=self._on_resume))
        elif state == STATE_ERROR:
            items.append(
                rumps.MenuItem(
                    "Fix Permissions", callback=self._on_fix_permissions
                )
            )
        return items

    def _voice_note_item(self, state: str) -> rumps.MenuItem:
        """Build the voice-note menu item for ``state``.

        See HELIOS.md §12.3:
        * armed                         → "Record Voice Note"
        * recording (calendar/cont.)    → "Record Voice Note (excerpt)"
        * recording_voice_note          → "Stop Voice Note" (overrides)
        """
        if state == STATE_RECORDING_VOICE_NOTE:
            return rumps.MenuItem(
                "Stop Voice Note", callback=self._on_voice_note_stop
            )
        if state == STATE_RECORDING:
            return rumps.MenuItem(
                "Record Voice Note (excerpt)",
                callback=self._on_voice_note_start,
            )
        # armed / paused / error all use the plain label. Hotkey hint
        # appended when configured so the user sees the binding.
        title = "Record Voice Note"
        if self._config.voice_note.hotkey_enabled:
            combo = self._config.voice_note.hotkey_combo
            title = f"{title} ({combo})"
        return rumps.MenuItem(title, callback=self._on_voice_note_start)

    def _screen_capture_submenu(self) -> rumps.MenuItem:
        """Build the "Capture Screen" submenu with fixed durations."""
        submenu = rumps.MenuItem("Capture Screen")
        for label, minutes in (
            ("5 min", 5),
            ("15 min", 15),
            ("30 min", 30),
            ("1 hour", 60),
        ):
            cb = self._make_screen_capture_callback(minutes)
            submenu.add(rumps.MenuItem(label, callback=cb))
        return submenu

    def _make_screen_capture_callback(
        self, minutes: int
    ) -> Callable[[Any], None]:
        """Closure factory for screen capture submenu items."""
        def _cb(_item: Any) -> None:
            self._dispatch_capture_start(
                kind="manual_screen", duration_minutes=minutes
            )
        return _cb

    def _pause_submenu(self) -> rumps.MenuItem:
        """Build the "Pause Capture" submenu with dynamic labels.

        See HELIOS.md §12.4 + Task 4B.3.
        """
        submenu = rumps.MenuItem("Pause Capture")
        morning_hour = int(self._config.capture.pause_morning_hour)
        now = datetime.now().astimezone()
        for label, target_dt in pause_submenu_options(now, morning_hour):
            cb = self._make_pause_callback(target_dt.timestamp())
            submenu.add(rumps.MenuItem(label, callback=cb))
        return submenu

    def _make_pause_callback(
        self, until_ts: float
    ) -> Callable[[Any], None]:
        """Closure factory for pause submenu items."""
        def _cb(_item: Any) -> None:
            self._dispatch_pause_until(until_ts)
        return _cb

    # ------------------------------------------------------------------
    # Click handlers — primary actions
    # ------------------------------------------------------------------

    def _on_start_daemon(self, _item: Any) -> None:
        """Start the LaunchAgent (was: not_running → loaded)."""
        ok, msg = launchctl_helpers.load_daemon()
        if not ok:
            self._show_error_alert(
                "Could not start Helios daemon", msg or ""
            )
            return
        # Force an immediate poll on the next runloop tick so the icon
        # updates without waiting 3s.
        self._poll(self._poll_timer)

    def _on_start_continuous(self, _item: Any) -> None:
        """Optimistic start — flip icon, dispatch in background, revert on fail."""
        prev_state = self._last_state
        self._set_icon(STATE_RECORDING)
        self._last_state = STATE_RECORDING
        self._rebuild_menu()

        def _worker() -> None:
            try:
                self._client.post_capture_start(kind="continuous")
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "start_continuous_failed",
                    error_type=type(exc).__name__,
                )
                # Revert on the runloop thread via rumps.Timer trick: the
                # next poll will pick up the truth, but we proactively
                # revert the icon so the user sees the failure quickly.
                self._set_icon(prev_state or STATE_ARMED)
                self._last_state = prev_state
                self._rebuild_menu()
                self._show_error_alert(
                    "Could not start continuous capture",
                    type(exc).__name__,
                )

        threading.Thread(
            target=_worker,
            name="helios-start-continuous",
            daemon=True,
        ).start()

    def _on_stop_capture(self, _item: Any) -> None:
        """Stop whatever's currently recording (calendar, continuous, manual)."""

        def _worker() -> None:
            try:
                self._client.post_capture_stop()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "stop_capture_failed",
                    error_type=type(exc).__name__,
                )
                self._show_error_alert(
                    "Could not stop capture", type(exc).__name__
                )

        threading.Thread(
            target=_worker, name="helios-stop-capture", daemon=True
        ).start()

    def _on_resume(self, _item: Any) -> None:
        def _worker() -> None:
            try:
                self._client.post_capture_resume()
            except Exception as exc:  # noqa: BLE001
                _log.warning("resume_failed", error_type=type(exc).__name__)
                self._show_error_alert(
                    "Could not resume", type(exc).__name__
                )

        threading.Thread(
            target=_worker, name="helios-resume", daemon=True
        ).start()

    def _on_fix_permissions(self, _item: Any) -> None:
        """Error state → user needs to fix permissions. Open onboarding."""
        # Re-running onboarding takes the user back through the mic /
        # screen permission steps which is exactly what the error state
        # usually means. Fall back to a deep-link if onboarding fails.
        try:
            window = OnboardingWindow(on_complete=lambda: None)
            window.show()
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning("fix_permissions_onboarding_failed", error=str(exc))
        # Fallback: open the screen-recording settings pane (most common
        # culprit). The user can adjust mic from the same alert chain.
        _open_url(DEEP_LINK_SCREEN)

    # ------------------------------------------------------------------
    # Click handlers — capture submenus
    # ------------------------------------------------------------------

    def _dispatch_capture_start(
        self, kind: str, duration_minutes: int | None = None
    ) -> None:
        def _worker() -> None:
            try:
                self._client.post_capture_start(
                    kind=kind, duration_minutes=duration_minutes
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "capture_start_failed",
                    kind=kind,
                    duration_minutes=duration_minutes,
                    error_type=type(exc).__name__,
                )
                self._show_error_alert(
                    "Could not start capture", type(exc).__name__
                )

        threading.Thread(
            target=_worker, name="helios-capture-start", daemon=True
        ).start()

    def _dispatch_pause_until(self, until_ts: float) -> None:
        def _worker() -> None:
            try:
                self._client.post_capture_pause_until(until_ts)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "pause_until_failed", error_type=type(exc).__name__
                )
                self._show_error_alert(
                    "Could not pause capture", type(exc).__name__
                )

        threading.Thread(
            target=_worker, name="helios-pause-until", daemon=True
        ).start()

    # ------------------------------------------------------------------
    # Click handlers — voice note
    # ------------------------------------------------------------------

    def _on_voice_note_start(self, _item: Any) -> None:
        """Trigger a voice-note start from the menu (Task 4B.6)."""
        self._trigger_voice_note_start(triggered_by="menu_bar")

    def _on_voice_note_stop(self, _item: Any) -> None:
        """Trigger a voice-note stop from the menu (Task 4B.6)."""
        self._trigger_voice_note_stop()

    def _trigger_voice_note_start(self, triggered_by: str) -> None:
        """Start a voice note + flip icon + spawn indicator optimistically."""
        prev_state = self._last_state
        # Optimistic icon flip — the indicator shows up on the next poll
        # tick that confirms the daemon has the note.
        self._set_icon(STATE_RECORDING_VOICE_NOTE)
        self._last_state = STATE_RECORDING_VOICE_NOTE

        def _worker() -> None:
            try:
                self._client.post_voice_note_start(triggered_by=triggered_by)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "voice_note_start_failed",
                    triggered_by=triggered_by,
                    error_type=type(exc).__name__,
                )
                self._set_icon(prev_state or STATE_ARMED)
                self._last_state = prev_state
                self._rebuild_menu()
                self._show_error_alert(
                    "Could not start voice note", type(exc).__name__
                )
                return
            # Indicator is created on the next poll tick once the daemon
            # confirms; we don't spawn it here to keep ownership clean.

        threading.Thread(
            target=_worker, name="helios-voice-note-start", daemon=True
        ).start()

    def _trigger_voice_note_stop(self) -> None:
        """Stop a voice note + open the save window with the response.

        The HTTP stop call blocks for several seconds (synchronous
        transcription), so we run it in a worker thread. But the save
        window is an NSWindow — construction MUST happen on the main
        thread or AppKit silently no-ops it (cycle 3 fix; user reported
        the save window never appeared after the stop button worked).

        Sets ``_voice_note_stop_handled_by_user`` so the next
        active→null transition in ``_poll`` doesn't double-trigger a
        force-stop handler.
        """
        self._voice_note_stop_handled_by_user = True

        def _worker() -> None:
            try:
                resp = self._client.post_voice_note_stop()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "voice_note_stop_failed",
                    error_type=type(exc).__name__,
                )
                # Bind exc into the lambda's defaults rather than capturing
                # by reference: Python clears ``exc`` when the ``except``
                # block exits, but the dispatched lambda runs later on the
                # main thread. A free-variable capture raises NameError
                # inside an NSBlockOperation → ObjC exception → SIGABRT
                # (menu-bar process crash, observed during §12.5 smoke).
                error_label = type(exc).__name__
                self._dispatch_to_main_thread(
                    lambda label=error_label: self._show_error_alert(
                        "Could not stop voice note", label
                    )
                )
                return
            self._dispatch_to_main_thread(
                lambda: self._open_save_window(resp)
            )

        threading.Thread(
            target=_worker, name="helios-voice-note-stop", daemon=True
        ).start()

    def _dispatch_to_main_thread(self, callable_: Any) -> None:
        """Run ``callable_`` on the AppKit main thread.

        Uses ``NSOperationQueue.mainQueue()`` so AppKit operations
        (NSWindow construction, NSAlert) don't fire from a worker
        thread (AppKit is not thread-safe). Falls back to direct call
        if PyObjC isn't available — in that case we're not on macOS
        anyway and the AppKit path won't be exercised.
        """
        try:
            from Foundation import NSThread, NSOperationQueue  # type: ignore[import-not-found]
            # If we're already on the main thread (e.g. tests running
            # synchronously, or the rumps runloop calling directly),
            # run inline. Otherwise dispatch via NSOperationQueue so
            # AppKit operations land on the right thread.
            if NSThread.isMainThread():
                callable_()
                return
            NSOperationQueue.mainQueue().addOperationWithBlock_(callable_)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "main_thread_dispatch_failed", error=str(exc)
            )
            try:
                callable_()
            except Exception:  # pragma: no cover
                pass

    def _spawn_indicator(self) -> None:
        """Create + show the floating indicator. Called when state flips
        to ``recording_voice_note``."""
        if self._indicator is not None:
            return
        try:
            self._indicator = VoiceNoteIndicator(
                client=self._client,
                config=self._config,
                persist_position=self._persist_indicator_position,
                # Indicator's Stop button must converge with the menu's
                # "Stop Voice Note" item — both flow through the same
                # _trigger_voice_note_stop, which calls stop AND opens
                # the save window. Without this, indicator-stop fires
                # the RPC but no save window ever appears (§12.5 cycle 3).
                stop_callback=self._trigger_voice_note_stop,
            )
            self._indicator.show()
        except RuntimeError as exc:
            # Non-mac dev / missing PyObjC. Don't crash.
            _log.warning("indicator_unavailable", error=str(exc))
            self._indicator = None
        except Exception as exc:  # noqa: BLE001
            _log.warning("indicator_show_failed", error=str(exc))
            self._indicator = None

    def _persist_indicator_position(self, pos: dict[str, int]) -> None:
        """Best-effort write-back of the indicator's drag-end position.

        Updates ``[voice_note.indicator] last_position`` in capture.toml
        so the next launch reopens the indicator where the user left it.
        Failures are logged at debug — the indicator is purely cosmetic
        and we never want a TOML write error to surface to the user.

        See HELIOS.md §12.10 acceptance: "User-draggable; final position
        persisted to ``voice_note.indicator.last_position``".
        """
        try:
            try:
                import tomllib  # py311+
            except ImportError:  # pragma: no cover
                import tomli as tomllib  # type: ignore[no-redef]

            path = Path("~/.aegis/capture.toml").expanduser()
            if not path.exists():
                # First run before the daemon ever started — nothing to
                # update. The next ``load_config`` call will create the
                # file with defaults; we'll persist on the next drag.
                return
            data = tomllib.loads(path.read_text())
            voice_note = data.setdefault("voice_note", {})
            indicator = voice_note.setdefault("indicator", {})
            indicator["last_position"] = {
                "x": int(pos.get("x", 0)),
                "y": int(pos.get("y", 0)),
            }
            # Re-serialize. ``tomllib`` is read-only, so we write a
            # minimal TOML by hand. Keep it simple: just rewrite the
            # affected section + preserve everything else as raw text
            # would require a round-tripping library we don't have. The
            # safe fallback is to use ``tomli_w`` if available; if not,
            # we update the in-memory config but skip the disk write.
            try:
                import tomli_w  # type: ignore[import-not-found]
            except ImportError:
                _log.debug(
                    "indicator_persist_skipped_no_tomli_w",
                    reason="tomli_w not installed",
                )
                # Still update the live config so the indicator at least
                # remembers the position within the current session.
                self._config.voice_note.indicator.last_position = (
                    indicator["last_position"]
                )
                return
            tmp = path.with_suffix(".toml.tmp")
            tmp.write_bytes(tomli_w.dumps(data).encode("utf-8"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            # Also keep the in-memory config in sync.
            self._config.voice_note.indicator.last_position = (
                indicator["last_position"]
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("indicator_persist_failed", error=type(exc).__name__)

    def _open_save_window(self, stop_response: Any) -> None:
        """Open the save window with the stopped voice-note metadata.

        ``stop_response`` is a :class:`VoiceNoteStopResponse`. We pull
        out the transcript text + ids and call
        :meth:`VoiceNoteSaveWindow.show` on the runloop.
        """
        # Extract transcript text. ``transcript`` may be None (Phase 2
        # placeholder) or have segments=[] — concatenate whatever we get.
        transcript_text = ""
        transcript = getattr(stop_response, "transcript", None)
        if transcript is not None:
            segments = getattr(transcript, "segments", None) or []
            transcript_text = " ".join(
                seg.text for seg in segments if getattr(seg, "text", "")
            ).strip()

        try:
            self._save_window = VoiceNoteSaveWindow(
                helios_client=self._client,
                aegis_base_url=_AEGIS_BASE_URL,
                config=self._config,
                on_close=self._on_save_window_close,
            )
            self._save_window.show(
                voice_note_id=stop_response.voice_note_id,
                helios_session_id=stop_response.session_id,
                transcript_text=transcript_text,
                started_at=stop_response.started_at,
                ended_at=stop_response.ended_at,
                duration_seconds=stop_response.duration_seconds,
                triggered_by="menu_bar",
                is_excerpt=bool(getattr(stop_response, "is_excerpt", False)),
            )
        except RuntimeError as exc:
            _log.warning("save_window_unavailable", error=str(exc))
            self._save_window = None
        except Exception as exc:  # noqa: BLE001
            _log.warning("save_window_show_failed", error=str(exc))
            self._save_window = None

    def _on_save_window_close(self) -> None:
        """Save window's on_close hook — drop our reference."""
        self._save_window = None

    # ------------------------------------------------------------------
    # Hotkey toggle
    # ------------------------------------------------------------------

    def _on_hotkey_trigger(self) -> None:
        """Hotkey fired — read current voice-note state and toggle.

        Runs on the Carbon event-handler thread (the macOS main run-loop
        for Carbon, not the rumps run-loop). We read the state via the
        sync client, then dispatch start/stop on a worker thread to keep
        the Carbon callback fast.
        """
        try:
            resp = self._client.get_active_voice_note()
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning(
                "hotkey_state_check_failed", error_type=type(exc).__name__
            )
            return

        if resp.active is not None:
            self._trigger_voice_note_stop()
        else:
            self._trigger_voice_note_start(triggered_by="hotkey")

    # ------------------------------------------------------------------
    # Click handlers — footer
    # ------------------------------------------------------------------

    def _on_open_dashboard(self, _item: Any) -> None:
        _open_url(_DASHBOARD_URL)

    def _on_open_preferences(self, _item: Any) -> None:
        _open_url(_DASHBOARD_SETTINGS_URL)

    def _on_rerun_onboarding(self, _item: Any) -> None:
        try:
            window = OnboardingWindow(on_complete=lambda: None)
            window.show()
        except Exception as exc:  # noqa: BLE001
            _log.warning("rerun_onboarding_failed", error=str(exc))
            self._show_error_alert(
                "Could not open onboarding", type(exc).__name__
            )

    def _on_quit_menu_bar(self, _item: Any) -> None:
        """Quit the menu bar but leave the daemon running."""
        try:
            rumps.quit_application()
        except Exception as exc:  # noqa: BLE001
            _log.warning("quit_failed", error=str(exc))

    def _on_stop_daemon(self, _item: Any) -> None:
        """Confirmation alert + stop the daemon via launchctl."""
        confirmed = _confirm_alert(
            title="Stop Helios Daemon?",
            message=(
                "This will end any active capture and unload the LaunchAgent. "
                "The menu bar app will stay open and can restart the daemon."
            ),
            ok="Stop Daemon",
            cancel="Cancel",
        )
        if not confirmed:
            return

        # Best-effort: ask the daemon to stop the active session first
        # so audio buffers flush. If the call fails the launchctl
        # bootout below still tears the process down.
        try:
            self._client.post_capture_stop()
        except Exception as exc:  # noqa: BLE001
            _log.info(
                "stop_daemon_capture_stop_failed",
                error_type=type(exc).__name__,
            )

        ok, msg = launchctl_helpers.unload_daemon()
        if not ok:
            self._show_error_alert("Could not stop daemon", msg or "")
            return
        # Trigger an immediate poll so the icon flips to not_running.
        self._poll(self._poll_timer)

    # ------------------------------------------------------------------
    # Notification callback wiring (Tasks 4C.3-5)
    # ------------------------------------------------------------------

    def _wire_notification_callbacks(self) -> None:
        """Register all action-button callbacks with the notifier."""
        # FOUR_HOUR_PROMPT — Continue keeps the session, Stop ends it.
        self._notifier.on_action(
            "FOUR_HOUR_PROMPT",
            "continue",
            lambda _ui: self._send_prompt_response(True),
        )
        self._notifier.on_action(
            "FOUR_HOUR_PROMPT",
            "stop",
            lambda _ui: self._send_prompt_response(False),
        )

        # PERMISSION_REVOKED — open the screen-recording settings.
        self._notifier.on_action(
            "PERMISSION_REVOKED",
            "open_settings",
            lambda _ui: _open_url(DEEP_LINK_SCREEN),
        )

        # MEETING_INTERRUPTED + MISSED_MEETING share the same action.
        self._notifier.on_action(
            "MEETING_INTERRUPTED",
            "open_settings",
            lambda _ui: _open_url(DEEP_LINK_SCREEN),
        )
        self._notifier.on_action(
            "MISSED_MEETING",
            "open_settings",
            lambda _ui: _open_url(DEEP_LINK_MIC),
        )

        # VOICE_NOTE_CAP_WARNING — Stop ends the note, Continue is a no-op.
        self._notifier.on_action(
            "VOICE_NOTE_CAP_WARNING",
            "stop",
            lambda _ui: self._trigger_voice_note_stop(),
        )
        self._notifier.on_action(
            "VOICE_NOTE_CAP_WARNING",
            "continue",
            lambda _ui: None,
        )

        # VOICE_NOTE_CAP_REACHED — save window opens automatically; the
        # action button is informational only.
        self._notifier.on_action(
            "VOICE_NOTE_CAP_REACHED",
            "open_save_window",
            lambda _ui: None,
        )

        # VOICE_NOTE_SAVE_FAILED — re-trigger save on the active window.
        self._notifier.on_action(
            "VOICE_NOTE_SAVE_FAILED",
            "retry",
            lambda _ui: self._retry_save_window(),
        )

        # VOICE_NOTE_HOTKEY_FAILED — open Accessibility settings.
        self._notifier.on_action(
            "VOICE_NOTE_HOTKEY_FAILED",
            "open_accessibility_settings",
            lambda _ui: _open_url(_DEEP_LINK_ACCESSIBILITY),
        )

    def _send_prompt_response(self, continue_session: bool) -> None:
        """POST the 4-hour-prompt response. Best-effort.

        Note: the daemon endpoint may not exist in older builds. We log
        and swallow the failure so the menu bar doesn't crash.
        """
        try:
            # Use the public client's request plumbing via a private
            # method — this isn't exposed on the client because the
            # endpoint shape isn't part of the menu bar's primary API
            # surface. On older daemons the call 404s; we treat as a
            # no-op so the notification dismisses cleanly.
            self._client._request(  # noqa: SLF001 — intentional bridge
                "POST",
                "/v1/capture/prompt-response",
                json={"continue": continue_session},
            )
        except Exception as exc:  # noqa: BLE001
            _log.info(
                "prompt_response_dispatch_failed",
                continue_session=continue_session,
                error_type=type(exc).__name__,
            )

    def _retry_save_window(self) -> None:
        """Re-attempt the save flow on the currently-open save window."""
        if self._save_window is None:
            _log.info("retry_save_no_window")
            return
        try:
            self._save_window.handle_save_clicked()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "retry_save_failed", error_type=type(exc).__name__
            )

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_load_config() -> HeliosConfig:
        """Best-effort config load — fall back to defaults on error.

        ``capture.toml`` may not exist on first launch (the daemon
        creates it on its own startup). When that happens, return the
        in-memory defaults so the menu bar can still render.
        """
        try:
            return load_config()
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning("config_load_failed", error_type=type(exc).__name__)
            try:
                return get_config()
            except Exception:  # noqa: BLE001
                return HeliosConfig()

    def _show_error_alert(self, title: str, body: str) -> None:
        """Show a non-blocking error alert. Best-effort."""
        try:
            rumps.alert(title=title, message=body, ok="OK")
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "error_alert_failed",
                title=title,
                error_type=type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_menubar() -> None:
    """Construct the rumps app, drive onboarding/hotkey, then run().

    Called from :mod:`helios.__main__` when the user invokes
    ``python -m helios`` without ``--daemon``.
    """
    app = HeliosApp()

    # Onboarding gate — start polling only after onboarding completes.
    showed_onboarding = app.maybe_show_onboarding()
    if not showed_onboarding:
        app.start_polling()

    # Hotkey is independent of onboarding; start it eagerly so the
    # binding is live as soon as the user dismisses any modal.
    app.maybe_start_hotkey()

    app.run()


# Backwards-compatibility alias — older callers reference this name.
HeliosMenuBarApp = HeliosApp


__all__ = [
    "HeliosApp",
    "HeliosMenuBarApp",
    "STATE_ARMED",
    "STATE_ERROR",
    "STATE_NOT_RUNNING",
    "STATE_PAUSED",
    "STATE_RECORDING",
    "STATE_RECORDING_VOICE_NOTE",
    "derive_state",
    "pause_submenu_options",
    "run_menubar",
]
