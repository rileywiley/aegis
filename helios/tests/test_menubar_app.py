"""Tests for the menu bar integrator app (Wave 2 — Track 4B + glue).

Strategy
--------

We mock the Wave 1 modules at the boundary — the test focuses on the
state→menu mapping, optimistic flips, and the integration glue.
``rumps`` itself is mocked at module-import time so we don't need a
real Cocoa runloop.

Areas covered (numbered per Wave 2 spec):

1. State transitions: armed → recording → paused → error → not_running
   each rebuilds the menu and switches the icon.
2. Optimistic UI: capture-start flip + revert on failure.
3. Voice-note flow integration: start → indicator created (on poll
   confirmation), stop → save window created.
4. Pause submenu: Mon-Thu shows 2 items, Fri-Sun shows 3.
5. Pause submenu: 2 AM Tue → "Until morning" (today); 10 AM Tue →
   "Until tomorrow morning".
6. Header click-through during recording (calendar) opens browser.
7. Header click during continuous recording is inert.
8. Notification callback: FOUR_HOUR_PROMPT.continue dispatches POST.
9. Daemon unreachable → menu shows "Start Capture Daemon".
10. Stop Helios Daemon: confirmation + unload.
11. Voice-note menu: "(excerpt)" suffix during calendar/continuous
    recording.
12. State ``recording_voice_note`` overrides regular ``recording``.

The PyObjC-dependent modules (NSAlert, deep-link, save window) are
patched via ``monkeypatch`` so the tests run on any platform.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from helios.api.schemas import (
    ActiveSessionResponse,
    ActiveVoiceNote,
    ActiveVoiceNoteResponse,
    PermissionsResponse,
    QueueCountsResponse,
    StatusResponse,
    VoiceNoteStopResponse,
)
from helios.menubar.app import (
    STATE_ARMED,
    STATE_ERROR,
    STATE_NOT_RUNNING,
    STATE_PAUSED,
    STATE_RECORDING,
    STATE_RECORDING_VOICE_NOTE,
    HeliosApp,
    derive_state,
    pause_submenu_options,
)
from helios.menubar.client import (
    DaemonAuthFailed,
    DaemonTimeout,
    DaemonUnreachable,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _permissions(mic: bool = True, screen: bool = True) -> PermissionsResponse:
    return PermissionsResponse(
        mic_granted=mic,
        screen_recording_granted=screen,
        last_checked_at=1700000000.0,
    )


def _make_status(
    *,
    mode: str = "armed",
    active_session: ActiveSessionResponse | None = None,
    paused_until: float | None = None,
) -> StatusResponse:
    return StatusResponse(
        daemon="running",
        mode=mode,  # type: ignore[arg-type]
        components={"audio_capture": "ok"},
        component_errors={},
        active_session=active_session,
        next_calendar_event=None,
        active_session_id=active_session.id if active_session else None,
        paused_until=paused_until,
        aegis_unreachable=False,
        last_error=None,
        queue=QueueCountsResponse(),
        scheduler_running=True,
        permissions=_permissions(),
    )


def _make_active_session(
    kind: str = "continuous",
    calendar_event_ids: list[str] | None = None,
) -> ActiveSessionResponse:
    return ActiveSessionResponse(
        id=42,
        kind=kind,  # type: ignore[arg-type]
        calendar_event_ids=calendar_event_ids or [],
        started_at=1700000000.0,
        screen_capture_override_until=None,
    )


def _make_config(
    *,
    hotkey_enabled: bool = False,
    hotkey_combo: str = "cmd+option+v",
    pause_morning_hour: int = 8,
) -> Any:
    """Minimal HeliosConfig stub. Tests don't need pydantic validation."""
    indicator = SimpleNamespace(
        floating_pill_position="top_right",
        last_position=None,
        show_elapsed_time=True,
        show_audio_level=True,
    )
    save_window = SimpleNamespace(auto_save_timeout_seconds=10)
    voice_note = SimpleNamespace(
        enabled=True,
        max_duration_seconds=300,
        soft_cap_notification=True,
        auto_save_timeout_seconds=10,
        default_save_action="save_with_suggestions",
        hotkey_enabled=hotkey_enabled,
        hotkey_combo=hotkey_combo,
        indicator=indicator,
        save_window=save_window,
    )
    capture = SimpleNamespace(pause_morning_hour=pause_morning_hour)
    return SimpleNamespace(voice_note=voice_note, capture=capture)


@pytest.fixture
def fake_client() -> MagicMock:
    """Mock HeliosClient. All methods return synthetic responses."""
    client = MagicMock(name="HeliosClient")
    client.get_status.return_value = _make_status(mode="armed")
    client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=None
    )
    return client


@pytest.fixture
def fake_notifier() -> MagicMock:
    """Mock MenuBarNotifier. Captures on_action() registrations."""
    notifier = MagicMock(name="MenuBarNotifier")
    notifier._registered = {}

    def _on_action(category, action, callback):
        notifier._registered[(category, action)] = callback

    notifier.on_action.side_effect = _on_action
    return notifier


@pytest.fixture
def app_factory(monkeypatch, fake_client, fake_notifier):
    """Factory that builds a HeliosApp with the supplied dependencies.

    Patches ``rumps.App.__init__`` so we don't actually try to build a
    Cocoa status item. Also patches ``rumps.Timer`` to a benign mock.
    """
    import rumps

    # Replace rumps.App.__init__ with a no-op for the duration of the
    # test — we still want the class hierarchy intact. Pre-seed the
    # private attributes that rumps property setters expect to read.
    def _fake_app_init(self, *a, **kw):
        self._name = "Helios"
        self._icon = None
        self._icon_nsimage = None
        self._title = None
        self._template = True
        self._quit_button = None
        # Provide a private menu the rumps property accessor reads.
        # We replace it with a magic mock per-app inside the factory.
        self._menu = MagicMock(name="rumps._menu_default")

    monkeypatch.setattr(rumps.App, "__init__", _fake_app_init)
    # ``icon`` setter calls ``self._nsapp.setStatusBarIcon()``. Default
    # rumps.App raises AttributeError there which is silently caught,
    # so we don't need to do anything — but we DO need to bypass the
    # ``_nsimage_from_file`` call which can fail on missing icons.
    # Patch the icon setter to a simple attribute write.
    def _fake_icon_setter(self, icon_path):
        self._icon = icon_path

    monkeypatch.setattr(
        rumps.App,
        "icon",
        property(lambda self: self._icon, _fake_icon_setter),
    )

    # Make ``self.menu`` settable / inspectable. rumps.App.menu is a
    # MenuItem-like object; a MagicMock gives us .add()/.clear() spy.
    fake_menu = MagicMock(name="rumps.menu")
    fake_menu._items = []

    def _add(item):
        fake_menu._items.append(item)
        return item

    fake_menu.add.side_effect = _add

    def _clear():
        fake_menu._items.clear()

    fake_menu.clear.side_effect = _clear

    # Replace Timer with a mock that captures args + start state.
    timer_instances: list[MagicMock] = []

    def _make_timer(callback, interval):
        t = MagicMock(name=f"rumps.Timer({interval})")
        t.callback = callback
        t.interval = interval
        t.is_alive.return_value = False
        t.start = MagicMock()
        t.stop = MagicMock()
        timer_instances.append(t)
        return t

    monkeypatch.setattr(rumps, "Timer", _make_timer)

    # Block actual NSAlert calls.
    monkeypatch.setattr(rumps, "alert", lambda **kw: 1)
    monkeypatch.setattr(
        rumps, "quit_application", lambda *a, **kw: None
    )

    # ``rumps.separator`` is a sentinel object; leave it intact.

    def _factory(
        *,
        client: Any = None,
        notifier: Any = None,
        config: Any = None,
    ):
        # Pre-seed `_menu` so the rumps property setter (which writes to
        # ``self._menu.update(...)``) doesn't blow up. It's normally set
        # in rumps.App.__init__ which we patched out.
        # We use a simple list-backed shim that the test inspects via
        # ``_test_menu``.
        app = HeliosApp(
            client=client if client is not None else fake_client,
            notifier=notifier if notifier is not None else fake_notifier,
            config=config if config is not None else _make_config(),
        )
        # Use a fresh fake menu per app instance so tests don't clash
        # if they instantiate multiple apps (the fixture is yielded once
        # per test, but defensive).
        per_app_menu = MagicMock(name="rumps.menu_per_app")
        per_app_menu._items = []
        per_app_menu.add.side_effect = lambda i: (
            per_app_menu._items.append(i) or i
        )
        per_app_menu.clear.side_effect = lambda: per_app_menu._items.clear()
        app._menu = per_app_menu  # type: ignore[attr-defined]
        # Capture menu on the app for test inspection convenience.
        app._test_menu = per_app_menu  # type: ignore[attr-defined]
        # Re-run the initial menu build now that ``_menu`` exists (the
        # constructor's first build silently fails because ``_menu`` was
        # missing — ``_rebuild_menu`` swallows that).
        app._rebuild_menu()
        return app

    yield _factory


# ---------------------------------------------------------------------------
# 0) derive_state state machine
# ---------------------------------------------------------------------------


def test_derive_state_unreachable():
    assert derive_state(None, voice_note_active=False) == STATE_NOT_RUNNING


def test_derive_state_armed():
    s = _make_status(mode="armed")
    assert derive_state(s, False) == STATE_ARMED


def test_derive_state_recording():
    s = _make_status(mode="recording", active_session=_make_active_session())
    assert derive_state(s, False) == STATE_RECORDING


def test_derive_state_paused():
    s = _make_status(mode="paused", paused_until=1800000000.0)
    assert derive_state(s, False) == STATE_PAUSED


def test_derive_state_error():
    s = _make_status(mode="error")
    assert derive_state(s, False) == STATE_ERROR


def test_derive_state_voice_note_overrides_recording():
    """Test #12: voice-note state wins over regular recording."""
    s = _make_status(mode="recording", active_session=_make_active_session())
    assert (
        derive_state(s, voice_note_active=True)
        == STATE_RECORDING_VOICE_NOTE
    )


def test_derive_state_voice_note_overrides_armed():
    s = _make_status(mode="armed")
    assert (
        derive_state(s, voice_note_active=True)
        == STATE_RECORDING_VOICE_NOTE
    )


# ---------------------------------------------------------------------------
# Test #1) State transitions rebuild menu + switch icon
# ---------------------------------------------------------------------------


def test_state_transition_armed_to_recording(app_factory, fake_client):
    """Polling armed → recording switches icon and rebuilds menu."""
    app = app_factory()

    # First poll: armed.
    fake_client.get_status.return_value = _make_status(mode="armed")
    app._poll(None)
    armed_items = list(app._test_menu._items)
    assert app._last_state == STATE_ARMED
    assert any("Start Continuous" in str(i.title) for i in armed_items if hasattr(i, "title"))

    # Second poll: recording.
    fake_client.get_status.return_value = _make_status(
        mode="recording", active_session=_make_active_session()
    )
    app._poll(None)
    rec_items = list(app._test_menu._items)
    assert app._last_state == STATE_RECORDING
    assert any("Stop Capture" in str(i.title) for i in rec_items if hasattr(i, "title"))


def test_state_transition_recording_to_paused(app_factory, fake_client):
    app = app_factory()

    fake_client.get_status.return_value = _make_status(
        mode="recording", active_session=_make_active_session()
    )
    app._poll(None)

    fake_client.get_status.return_value = _make_status(
        mode="paused", paused_until=1800000000.0
    )
    app._poll(None)

    assert app._last_state == STATE_PAUSED
    items = list(app._test_menu._items)
    assert any("Resume" in str(i.title) for i in items if hasattr(i, "title"))


def test_state_transition_to_error(app_factory, fake_client):
    app = app_factory()
    fake_client.get_status.return_value = _make_status(mode="error")
    app._poll(None)
    assert app._last_state == STATE_ERROR
    items = list(app._test_menu._items)
    assert any("Fix Permissions" in str(i.title) for i in items if hasattr(i, "title"))


# ---------------------------------------------------------------------------
# Test #9) Daemon unreachable → not_running primary action
# ---------------------------------------------------------------------------


def test_daemon_unreachable_shows_start_daemon(app_factory, fake_client):
    app = app_factory()
    fake_client.get_status.side_effect = DaemonUnreachable("nope")
    app._poll(None)
    assert app._last_state == STATE_NOT_RUNNING
    items = list(app._test_menu._items)
    assert any(
        "Start Capture Daemon" in str(i.title)
        for i in items
        if hasattr(i, "title")
    )


def test_daemon_timeout_treated_as_unreachable(app_factory, fake_client):
    app = app_factory()
    fake_client.get_status.side_effect = DaemonTimeout("slow")
    app._poll(None)
    assert app._last_state == STATE_NOT_RUNNING


def test_daemon_auth_failed_treated_as_unreachable(app_factory, fake_client):
    app = app_factory()
    fake_client.get_status.side_effect = DaemonAuthFailed("401")
    app._poll(None)
    assert app._last_state == STATE_NOT_RUNNING


# ---------------------------------------------------------------------------
# Test #2) Optimistic UI: capture-start flip + revert on failure
# ---------------------------------------------------------------------------


def test_optimistic_start_continuous_flips_icon(app_factory, fake_client):
    """Click → icon flips to recording immediately, before API returns."""
    import threading

    app = app_factory()
    # Pretend we're armed.
    app._last_state = STATE_ARMED

    # Block the post in a way we can release manually.
    block = threading.Event()
    release = threading.Event()

    def _slow_post(**kw):
        block.set()
        release.wait(timeout=2.0)
        return MagicMock()

    fake_client.post_capture_start.side_effect = _slow_post

    app._on_start_continuous(None)

    # Wait until the worker has begun the POST (so we know the icon
    # flip happened first).
    assert block.wait(timeout=2.0)
    assert app._last_state == STATE_RECORDING

    # Release the worker so the test cleans up.
    release.set()


def test_optimistic_start_continuous_reverts_on_failure(
    app_factory, fake_client
):
    """API failure reverts the icon back to the prior state."""
    import threading

    app = app_factory()
    app._last_state = STATE_ARMED

    # Make the API fail.
    fake_client.post_capture_start.side_effect = DaemonUnreachable("nope")

    # Patch the alert so we don't try to show one.
    app._show_error_alert = MagicMock()

    # Patch threading.Thread so the worker runs synchronously.
    real_thread = threading.Thread

    def _sync_thread(target=None, **kw):
        t = MagicMock()
        t.start = lambda: target() if target else None
        return t

    import helios.menubar.app as app_mod

    orig = app_mod.threading.Thread
    app_mod.threading.Thread = _sync_thread
    try:
        app._on_start_continuous(None)
    finally:
        app_mod.threading.Thread = orig

    # After the failure, state reverts to armed.
    assert app._last_state == STATE_ARMED
    app._show_error_alert.assert_called_once()


# ---------------------------------------------------------------------------
# Test #3) Voice-note flow integration: spawn indicator + open save window
# ---------------------------------------------------------------------------


def test_voice_note_active_spawns_indicator(
    app_factory, fake_client, monkeypatch
):
    """Polling voice_note_active=True spawns the indicator."""
    app = app_factory()

    # Replace VoiceNoteIndicator with a spy.
    import helios.menubar.app as app_mod

    indicator_spy = MagicMock(name="VoiceNoteIndicator")
    indicator_instance = MagicMock(name="indicator_instance")
    indicator_spy.return_value = indicator_instance
    monkeypatch.setattr(app_mod, "VoiceNoteIndicator", indicator_spy)

    fake_client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=ActiveVoiceNote(
            voice_note_id=1,
            session_id=1,
            started_at=1700000000.0,
            elapsed_seconds=5.0,
            is_excerpt=False,
            max_duration_seconds=300,
        )
    )
    app._poll(None)

    assert app._last_state == STATE_RECORDING_VOICE_NOTE
    indicator_spy.assert_called_once()
    indicator_instance.show.assert_called_once()


def test_voice_note_inactive_drops_indicator(
    app_factory, fake_client, monkeypatch
):
    """When voice note ends, app drops the indicator reference."""
    app = app_factory()

    # Pre-populate as if the indicator was running.
    app._indicator = MagicMock()
    app._last_state = STATE_RECORDING_VOICE_NOTE

    # Polling returns no active note.
    fake_client.get_status.return_value = _make_status(mode="armed")
    fake_client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=None
    )
    app._poll(None)

    assert app._last_state == STATE_ARMED
    assert app._indicator is None


def test_voice_note_stop_opens_save_window(
    app_factory, fake_client, monkeypatch
):
    """Stop click → POST stop → open save window with the response."""
    app = app_factory()

    # Patch save window class.
    import helios.menubar.app as app_mod

    save_window_spy = MagicMock(name="VoiceNoteSaveWindow")
    save_window_instance = MagicMock()
    save_window_spy.return_value = save_window_instance
    monkeypatch.setattr(app_mod, "VoiceNoteSaveWindow", save_window_spy)

    # Run worker thread synchronously.
    monkeypatch.setattr(
        app_mod.threading,
        "Thread",
        lambda target=None, **kw: SimpleNamespace(
            start=lambda: target() if target else None
        ),
    )

    stop_resp = VoiceNoteStopResponse(
        voice_note_id=99,
        session_id=42,
        started_at=1700000000.0,
        ended_at=1700000034.0,
        duration_seconds=34.0,
        transcript=None,
        is_excerpt=False,
    )
    fake_client.post_voice_note_stop.return_value = stop_resp

    app._trigger_voice_note_stop()

    save_window_spy.assert_called_once()
    save_window_instance.show.assert_called_once()
    kwargs = save_window_instance.show.call_args.kwargs
    assert kwargs["voice_note_id"] == 99
    assert kwargs["helios_session_id"] == 42
    assert kwargs["duration_seconds"] == 34.0


# ---------------------------------------------------------------------------
# Test #4) Pause submenu Mon-Thu vs Fri-Sun
# ---------------------------------------------------------------------------


def test_pause_submenu_monday_to_thursday():
    """Mon (0) through Thu (3): only 1 hour + morning."""
    for weekday in range(0, 4):
        # Pick a 10 AM date for each weekday (after morning hour).
        # 2026-01-05 is a Monday.
        base = datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
        from datetime import timedelta as _td

        now = base + _td(days=weekday)
        opts = pause_submenu_options(now, morning_hour=8)
        assert len(opts) == 2, f"weekday {weekday}: expected 2 got {len(opts)}"
        labels = [o[0] for o in opts]
        assert "1 hour" in labels
        assert any("morning" in label.lower() for label in labels)
        assert not any("Monday" in label for label in labels)


def test_pause_submenu_fri_sat_sun():
    """Fri (4), Sat (5), Sun (6): 3 items including Until Monday morning."""
    for weekday in range(4, 7):
        # 2026-01-09 is Friday.
        from datetime import timedelta as _td

        base = datetime(2026, 1, 9, 10, 0, 0, tzinfo=timezone.utc)
        now = base + _td(days=weekday - 4)
        opts = pause_submenu_options(now, morning_hour=8)
        assert len(opts) == 3, f"weekday {weekday}: expected 3 got {len(opts)}"
        labels = [o[0] for o in opts]
        assert "Until Monday morning" in labels


# ---------------------------------------------------------------------------
# Test #5) Pause submenu: time-of-day affects "morning" label
# ---------------------------------------------------------------------------


def test_pause_submenu_2am_tuesday_until_morning():
    """At 2 AM Tue, the morning option says 'Until morning' (today)."""
    # 2026-01-06 is Tuesday.
    now = datetime(2026, 1, 6, 2, 0, 0, tzinfo=timezone.utc)
    opts = pause_submenu_options(now, morning_hour=8)
    labels = [o[0] for o in opts]
    assert "Until morning" in labels
    # Target should be today at 8 AM.
    morning_dt = next(dt for label, dt in opts if label == "Until morning")
    assert morning_dt.day == 6
    assert morning_dt.hour == 8


def test_pause_submenu_10am_tuesday_until_tomorrow():
    """At 10 AM Tue (after 8 AM), the morning option is 'Until tomorrow morning'."""
    now = datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
    opts = pause_submenu_options(now, morning_hour=8)
    labels = [o[0] for o in opts]
    assert "Until tomorrow morning" in labels
    morning_dt = next(
        dt for label, dt in opts if label == "Until tomorrow morning"
    )
    assert morning_dt.day == 7
    assert morning_dt.hour == 8


# ---------------------------------------------------------------------------
# Test #6 + #7) Header click during recording (calendar vs continuous)
# ---------------------------------------------------------------------------


def test_header_click_recording_calendar_opens_meeting(
    app_factory, fake_client, monkeypatch
):
    """Recording calendar → header click opens Aegis meeting URL."""
    import helios.menubar.app as app_mod

    opened: list[str] = []
    monkeypatch.setattr(app_mod, "_open_url", lambda url: opened.append(url))

    app = app_factory()
    session = _make_active_session(
        kind="calendar", calendar_event_ids=["evt-meeting-123"]
    )
    fake_client.get_status.return_value = _make_status(
        mode="recording", active_session=session
    )
    app._poll(None)

    items = list(app._test_menu._items)
    header = next(i for i in items if hasattr(i, "title") and "Recording" in str(i.title))
    cb = getattr(header, "callback", None)
    assert cb is not None
    cb(header)
    assert any("evt-meeting-123" in url for url in opened)


def test_header_click_recording_continuous_is_inert(
    app_factory, fake_client, monkeypatch
):
    """Continuous recording → header has no callback."""
    import helios.menubar.app as app_mod

    opened: list[str] = []
    monkeypatch.setattr(app_mod, "_open_url", lambda url: opened.append(url))

    app = app_factory()
    session = _make_active_session(kind="continuous", calendar_event_ids=[])
    fake_client.get_status.return_value = _make_status(
        mode="recording", active_session=session
    )
    app._poll(None)

    items = list(app._test_menu._items)
    header = next(i for i in items if hasattr(i, "title") and "Recording" in str(i.title))
    cb = (
        header.callback
        if hasattr(header, "callback")
        else getattr(header, "_callback", None)
    )
    assert cb is None
    assert opened == []


# ---------------------------------------------------------------------------
# Test #8) FOUR_HOUR_PROMPT.continue dispatches POST
# ---------------------------------------------------------------------------


def test_notification_callbacks_registered(app_factory, fake_notifier):
    app = app_factory()
    # All required (category, action) pairs registered.
    expected = {
        ("FOUR_HOUR_PROMPT", "continue"),
        ("FOUR_HOUR_PROMPT", "stop"),
        ("PERMISSION_REVOKED", "open_settings"),
        ("MEETING_INTERRUPTED", "open_settings"),
        ("MISSED_MEETING", "open_settings"),
        ("VOICE_NOTE_CAP_WARNING", "stop"),
        ("VOICE_NOTE_CAP_WARNING", "continue"),
        ("VOICE_NOTE_CAP_REACHED", "open_save_window"),
        ("VOICE_NOTE_SAVE_FAILED", "retry"),
        ("VOICE_NOTE_HOTKEY_FAILED", "open_accessibility_settings"),
    }
    assert expected.issubset(set(fake_notifier._registered.keys()))


def test_four_hour_prompt_continue_dispatches_post(
    app_factory, fake_client, fake_notifier
):
    """FOUR_HOUR_PROMPT.continue callback sends POST with continue=True."""
    app = app_factory()
    cb = fake_notifier._registered[("FOUR_HOUR_PROMPT", "continue")]
    # Reset call history from app construction.
    fake_client._request.reset_mock()
    cb({})
    fake_client._request.assert_called_once()
    args, kwargs = fake_client._request.call_args
    assert args[0] == "POST"
    assert args[1] == "/v1/capture/prompt-response"
    assert kwargs["json"] == {"continue": True}


def test_four_hour_prompt_stop_dispatches_post(
    app_factory, fake_client, fake_notifier
):
    app = app_factory()
    cb = fake_notifier._registered[("FOUR_HOUR_PROMPT", "stop")]
    fake_client._request.reset_mock()
    cb({})
    fake_client._request.assert_called_once()
    _, kwargs = fake_client._request.call_args
    assert kwargs["json"] == {"continue": False}


def test_voice_note_cap_warning_stop_triggers_stop(
    app_factory, fake_client, fake_notifier, monkeypatch
):
    """VOICE_NOTE_CAP_WARNING.stop fires the stop pipeline."""
    app = app_factory()

    # Run the worker synchronously so we can assert the call.
    import helios.menubar.app as app_mod

    monkeypatch.setattr(
        app_mod.threading,
        "Thread",
        lambda target=None, **kw: SimpleNamespace(
            start=lambda: target() if target else None
        ),
    )

    fake_client.post_voice_note_stop.return_value = VoiceNoteStopResponse(
        voice_note_id=1,
        session_id=1,
        started_at=1.0,
        ended_at=2.0,
        duration_seconds=1.0,
        transcript=None,
        is_excerpt=False,
    )

    cb = fake_notifier._registered[("VOICE_NOTE_CAP_WARNING", "stop")]
    cb({})

    fake_client.post_voice_note_stop.assert_called_once()


def test_hotkey_failed_opens_accessibility(
    app_factory, fake_notifier, monkeypatch
):
    """VOICE_NOTE_HOTKEY_FAILED.open_accessibility_settings opens the URL."""
    import helios.menubar.app as app_mod

    opened: list[str] = []
    monkeypatch.setattr(app_mod, "_open_url", lambda url: opened.append(url))

    app = app_factory()
    cb = fake_notifier._registered[
        ("VOICE_NOTE_HOTKEY_FAILED", "open_accessibility_settings")
    ]
    cb({})
    assert any("Accessibility" in url for url in opened)


# ---------------------------------------------------------------------------
# Test #10) Stop Helios Daemon: confirmation + unload
# ---------------------------------------------------------------------------


def test_stop_daemon_with_confirmation(
    app_factory, fake_client, monkeypatch
):
    """Stop Helios Daemon shows alert; OK → unload_daemon()."""
    import helios.menubar.app as app_mod

    monkeypatch.setattr(
        app_mod, "_confirm_alert", lambda **kw: True
    )

    unload_calls: list[Any] = []

    def _fake_unload():
        unload_calls.append(True)
        return (True, "")

    monkeypatch.setattr(
        app_mod.launchctl_helpers, "unload_daemon", _fake_unload
    )

    app = app_factory()
    app._on_stop_daemon(None)

    assert unload_calls == [True]


def test_stop_daemon_cancel_no_unload(
    app_factory, fake_client, monkeypatch
):
    """User clicks Cancel → no unload."""
    import helios.menubar.app as app_mod

    monkeypatch.setattr(
        app_mod, "_confirm_alert", lambda **kw: False
    )

    unload_calls: list[Any] = []
    monkeypatch.setattr(
        app_mod.launchctl_helpers,
        "unload_daemon",
        lambda: (unload_calls.append(True), (True, ""))[1],
    )

    app = app_factory()
    app._on_stop_daemon(None)

    assert unload_calls == []


# ---------------------------------------------------------------------------
# Test #11) Voice-note menu: "(excerpt)" suffix during calendar/continuous
# ---------------------------------------------------------------------------


def test_voice_note_menu_excerpt_during_recording(app_factory, fake_client):
    """Recording state → 'Record Voice Note (excerpt)' label."""
    app = app_factory()
    fake_client.get_status.return_value = _make_status(
        mode="recording", active_session=_make_active_session()
    )
    app._poll(None)

    items = list(app._test_menu._items)
    titles = [str(i.title) for i in items if hasattr(i, "title")]
    assert any("(excerpt)" in t for t in titles)


def test_voice_note_menu_plain_when_armed(app_factory, fake_client):
    """Armed state → plain 'Record Voice Note' (no excerpt)."""
    app = app_factory()
    fake_client.get_status.return_value = _make_status(mode="armed")
    app._poll(None)

    items = list(app._test_menu._items)
    titles = [str(i.title) for i in items if hasattr(i, "title")]
    assert any(
        t.startswith("Record Voice Note") and "(excerpt)" not in t
        for t in titles
    )


def test_voice_note_menu_stop_during_voice_note(
    app_factory, fake_client
):
    """Voice-note state → 'Stop Voice Note' menu item."""
    app = app_factory()
    fake_client.get_status.return_value = _make_status(mode="armed")
    fake_client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=ActiveVoiceNote(
            voice_note_id=1,
            session_id=1,
            started_at=1700000000.0,
            elapsed_seconds=5.0,
            is_excerpt=False,
            max_duration_seconds=300,
        )
    )
    # Patch indicator class to a no-op so we don't crash on PyObjC.
    import helios.menubar.app as app_mod

    app_mod.VoiceNoteIndicator = lambda **kw: MagicMock(show=lambda: None)
    app._poll(None)

    items = list(app._test_menu._items)
    titles = [str(i.title) for i in items if hasattr(i, "title")]
    assert any("Stop Voice Note" in t for t in titles)


# ---------------------------------------------------------------------------
# Test #12) Icon flips when state changes (specifically for voice note)
# ---------------------------------------------------------------------------


def test_voice_note_state_sets_voice_note_icon(
    app_factory, fake_client, monkeypatch
):
    """Voice-note state triggers _set_icon('recording_voice_note')."""
    app = app_factory()

    # Spy on _set_icon (it's called twice; we check the call list).
    set_calls: list[str] = []
    orig = app._set_icon
    app._set_icon = lambda state: set_calls.append(state)

    # Patch indicator to no-op.
    import helios.menubar.app as app_mod

    app_mod.VoiceNoteIndicator = lambda **kw: MagicMock(show=lambda: None)

    fake_client.get_status.return_value = _make_status(
        mode="recording", active_session=_make_active_session()
    )
    fake_client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=ActiveVoiceNote(
            voice_note_id=1,
            session_id=1,
            started_at=1700000000.0,
            elapsed_seconds=5.0,
            is_excerpt=False,
            max_duration_seconds=300,
        )
    )
    app._poll(None)

    assert STATE_RECORDING_VOICE_NOTE in set_calls


# ---------------------------------------------------------------------------
# Misc: hotkey gate
# ---------------------------------------------------------------------------


def test_hotkey_disabled_skips_setup(app_factory):
    """hotkey_enabled=False → no VoiceNoteHotkey constructed."""
    app = app_factory(config=_make_config(hotkey_enabled=False))
    app.maybe_start_hotkey()
    assert app._hotkey is None


def test_hotkey_enabled_invalid_combo_logged(
    app_factory, monkeypatch
):
    """Bad combo string fails gracefully — no crash, _hotkey stays None."""
    app = app_factory(
        config=_make_config(hotkey_enabled=True, hotkey_combo="not-a-combo")
    )
    app.maybe_start_hotkey()
    assert app._hotkey is None


def test_hotkey_enabled_valid_combo_constructs(
    app_factory, monkeypatch
):
    """Valid combo + hotkey_enabled → VoiceNoteHotkey constructed."""
    import helios.menubar.app as app_mod

    constructed: list[Any] = []

    def _stub_hotkey(combo, on_trigger, clock):
        constructed.append((combo, on_trigger, clock))
        h = MagicMock(name="VoiceNoteHotkey")
        # ``start`` is normally a coroutine, but ``run_coroutine_threadsafe``
        # is patched out below, so we never await it. Provide a plain
        # callable to avoid "coroutine was never awaited" warnings.
        h.start = MagicMock(return_value=None)
        return h

    monkeypatch.setattr(app_mod, "VoiceNoteHotkey", _stub_hotkey)

    # Don't actually spin up the asyncio loop in tests.
    monkeypatch.setattr(
        app_mod.threading,
        "Thread",
        lambda target=None, **kw: SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr(
        app_mod.asyncio,
        "run_coroutine_threadsafe",
        lambda *a, **kw: MagicMock(),
    )

    app = app_factory(
        config=_make_config(hotkey_enabled=True, hotkey_combo="cmd+option+v")
    )
    app.maybe_start_hotkey()
    assert len(constructed) == 1
    assert constructed[0][0] == "cmd+option+v"
    # Idempotent — second call doesn't re-construct.
    app.maybe_start_hotkey()
    assert len(constructed) == 1


def test_hotkey_trigger_starts_when_inactive(
    app_factory, fake_client, monkeypatch
):
    """Hotkey fires while inactive → start voice note."""
    import helios.menubar.app as app_mod

    monkeypatch.setattr(
        app_mod.threading,
        "Thread",
        lambda target=None, **kw: SimpleNamespace(
            start=lambda: target() if target else None
        ),
    )

    app = app_factory()
    fake_client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=None
    )
    fake_client.post_voice_note_start.return_value = MagicMock(
        voice_note_id=1
    )

    app._on_hotkey_trigger()

    fake_client.post_voice_note_start.assert_called_once()
    kwargs = fake_client.post_voice_note_start.call_args.kwargs
    assert kwargs["triggered_by"] == "hotkey"


def test_hotkey_trigger_stops_when_active(
    app_factory, fake_client, monkeypatch
):
    """Hotkey fires while active → stop voice note."""
    import helios.menubar.app as app_mod

    monkeypatch.setattr(
        app_mod.threading,
        "Thread",
        lambda target=None, **kw: SimpleNamespace(
            start=lambda: target() if target else None
        ),
    )

    app = app_factory()
    fake_client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=ActiveVoiceNote(
            voice_note_id=7,
            session_id=7,
            started_at=1700000000.0,
            elapsed_seconds=12.0,
            is_excerpt=False,
            max_duration_seconds=300,
        )
    )
    fake_client.post_voice_note_stop.return_value = VoiceNoteStopResponse(
        voice_note_id=7,
        session_id=7,
        started_at=1.0,
        ended_at=13.0,
        duration_seconds=12.0,
        transcript=None,
        is_excerpt=False,
    )

    # Patch save window so it doesn't try AppKit.
    monkeypatch.setattr(
        app_mod, "VoiceNoteSaveWindow", lambda **kw: MagicMock()
    )

    app._on_hotkey_trigger()

    fake_client.post_voice_note_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Onboarding gate
# ---------------------------------------------------------------------------


def test_maybe_show_onboarding_skips_when_complete(
    app_factory, monkeypatch
):
    import helios.menubar.app as app_mod

    monkeypatch.setattr(app_mod, "is_onboarding_complete", lambda: True)
    monkeypatch.setattr(
        app_mod, "OnboardingWindow", MagicMock(side_effect=AssertionError)
    )

    app = app_factory()
    assert app.maybe_show_onboarding() is False


def test_maybe_show_onboarding_shows_when_incomplete(
    app_factory, monkeypatch
):
    import helios.menubar.app as app_mod

    monkeypatch.setattr(app_mod, "is_onboarding_complete", lambda: False)

    instances: list[Any] = []

    def _make_window(on_complete):
        w = MagicMock(name="OnboardingWindow")
        w.show = MagicMock()
        instances.append((w, on_complete))
        return w

    monkeypatch.setattr(app_mod, "OnboardingWindow", _make_window)

    app = app_factory()
    assert app.maybe_show_onboarding() is True
    assert len(instances) == 1
    instances[0][0].show.assert_called_once()


def test_maybe_show_onboarding_runtime_error_falls_back(
    app_factory, monkeypatch
):
    """OnboardingWindow raises (no AppKit) → returns False, no crash."""
    import helios.menubar.app as app_mod

    monkeypatch.setattr(app_mod, "is_onboarding_complete", lambda: False)

    def _bad_window(**kw):
        raise RuntimeError("no AppKit")

    monkeypatch.setattr(app_mod, "OnboardingWindow", _bad_window)

    app = app_factory()
    assert app.maybe_show_onboarding() is False
