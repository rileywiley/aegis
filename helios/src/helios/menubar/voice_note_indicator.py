"""Floating voice-note recording indicator (Track 4G).

Per HELIOS.md §12.10 the menu bar app displays a small floating window
while a voice note is recording. It shows:

* Pulsing red dot (timer-based redraw, CPU-cheap)
* Elapsed MM:SS time
* Audio level bar driven by ``current_rms`` from the daemon
* Stop button
* Color shifts to amber when ``approaching_cap=true``

The indicator polls ``/v1/voice-note/active`` every 250ms via the
injected :class:`HeliosClient`. Polling stops automatically when the
voice note ends (``active=null``) or the daemon goes unreachable —
either path calls :meth:`VoiceNoteIndicator.dismiss`.

Architecture
------------

* SYNC. Lives on the rumps run-loop. NSWindow + NSTimer all dispatch
  there.
* PyObjC is **lazy-imported** inside :meth:`show`. The module imports
  cleanly on non-mac (so the test suite can run on CI Linux). On mac
  without PyObjC available, ``show()`` raises a clean ``RuntimeError``.
* Position is read from ``config.voice_note.indicator.floating_pill_position``.
  Default ``"top_right"`` ⇒ 10px from the top-right of the primary
  display. Custom value can be a string or a ``{"x": int, "y": int}``
  dict. Drag-end persists the new position via an injectable
  ``persist_position`` callable so tests don't touch capture.toml.

Logging is PII-safe — only counters, status flags, and scalar RMS.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from helios.log import get_logger

_log = get_logger("menubar.voice_note_indicator")


# Default indicator window dimensions. ~220×60px is enough for the dot,
# MM:SS clock, level bar, and stop button without dominating the screen.
_INDICATOR_WIDTH = 220
_INDICATOR_HEIGHT = 60
_DEFAULT_TOP_RIGHT_INSET = 10  # px from the screen edge

# Polling cadence per HELIOS.md §12.10. 250ms keeps the level meter
# responsive without burning CPU.
_POLL_INTERVAL_SECONDS = 0.25


class _PyObjCImports:
    """Holds the lazy PyObjC handles. Populated by :func:`_import_appkit`."""

    AppKit: Any = None
    Foundation: Any = None
    objc: Any = None


def _import_appkit() -> _PyObjCImports:
    """Lazy-import PyObjC. Raises ``RuntimeError`` on import failure."""
    try:
        import AppKit  # type: ignore[import-not-found]
        import Foundation  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PyObjC (AppKit/Foundation) not available — VoiceNoteIndicator "
            "requires macOS"
        ) from exc
    bundle = _PyObjCImports()
    bundle.AppKit = AppKit
    bundle.Foundation = Foundation
    bundle.objc = objc
    return bundle


def _resolve_position(
    position: Any,
    screen_size: tuple[int, int],
    window_size: tuple[int, int] = (_INDICATOR_WIDTH, _INDICATOR_HEIGHT),
) -> tuple[int, int]:
    """Resolve a config position value into absolute (x, y) coords.

    Accepts:

    * ``"top_right"`` (default) — 10px inset from primary display's top-right
    * ``"top_left"``
    * ``"bottom_right"``
    * ``"bottom_left"``
    * ``{"x": int, "y": int}`` — explicit coords (used when persisting after
      a user drag)

    Coordinates use Cocoa's bottom-left origin so they can be passed
    directly to ``NSWindow.setFrameOrigin_``.
    """
    sw, sh = screen_size
    ww, wh = window_size

    if isinstance(position, dict):
        x = position.get("x")
        y = position.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return int(x), int(y)
        # Fall through to default on malformed dict.
        position = "top_right"

    if not isinstance(position, str):
        position = "top_right"

    inset = _DEFAULT_TOP_RIGHT_INSET
    if position == "top_left":
        return inset, sh - wh - inset
    if position == "bottom_left":
        return inset, inset
    if position == "bottom_right":
        return sw - ww - inset, inset
    # Default top_right.
    return sw - ww - inset, sh - wh - inset


def _make_stop_button_target_class(appkit: Any, objc: Any) -> Any:
    """Build (or reuse) the NSObject subclass used as the stop button target.

    AppKit calls ``[target action:sender]`` on the registered selector.
    PyObjC's :func:`objc.selector` lets us expose a Python callable to
    Cocoa, but the *target* object itself must inherit from NSObject —
    Cocoa won't invoke selectors on raw Python objects (that was Bug 2
    in §12.5 smoke). We mirror the pattern used in
    :func:`helios.menubar.onboarding._make_button_target_class`.

    The class is cached on the AppKit module so we don't re-register
    it on every :meth:`VoiceNoteIndicator.show`.
    """
    cached = getattr(appkit, "_HeliosVoiceNoteStopTarget", None)
    if cached is not None:
        return cached

    NSObject = appkit.NSObject

    class HeliosVoiceNoteStopTarget(NSObject):  # type: ignore[misc]
        def initWithBridge_(self, bridge):  # noqa: N802 - ObjC selector name
            self = objc.super(HeliosVoiceNoteStopTarget, self).init()
            if self is None:
                return None
            self._bridge = bridge
            return self

        def doAction_(self, _sender):  # noqa: N802 - ObjC selector name
            try:
                self._bridge()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "voice_note_stop_button_handler_failed",
                    error=str(exc),
                )

    setattr(appkit, "_HeliosVoiceNoteStopTarget", HeliosVoiceNoteStopTarget)
    return HeliosVoiceNoteStopTarget


def _make_timer_target_class(appkit: Any, objc: Any) -> Any:
    """Build the NSObject subclass used as NSTimer's target.

    Same NSObject-bridge pattern as the stop button (Bug 2 in §12.5
    cycle 2): NSTimer.scheduledTimerWithTimeInterval_target_selector_…
    requires the target to be an NSObject. Passing a plain Python
    object silently no-ops every fire — the timer schedules but the
    selector never invokes our handler. That made the poll/pulse
    timers fail to update the elapsed-time label and pulse the dot.
    """
    cached = getattr(appkit, "_HeliosVoiceNoteTimerTarget", None)
    if cached is not None:
        return cached

    NSObject = appkit.NSObject

    class HeliosVoiceNoteTimerTarget(NSObject):  # type: ignore[misc]
        def initWithBridge_(self, bridge):  # noqa: N802 - ObjC selector name
            self = objc.super(HeliosVoiceNoteTimerTarget, self).init()
            if self is None:
                return None
            self._bridge = bridge
            return self

        def timerFired_(self, _timer):  # noqa: N802 - ObjC selector name
            try:
                self._bridge()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "voice_note_indicator_timer_handler_failed",
                    error=str(exc),
                )

    setattr(appkit, "_HeliosVoiceNoteTimerTarget", HeliosVoiceNoteTimerTarget)
    return HeliosVoiceNoteTimerTarget


def _make_indicator_background_class(appkit: Any, foundation: Any = None) -> Any:
    """Build (or reuse) the NSView subclass that paints the rounded background.

    The window is borderless + transparent (``setOpaque_(False)``) so we
    can have rounded corners outside the rect. The background view's
    ``drawRect_`` paints a solid red (or amber, when the indicator's
    ``color_state`` is ``"amber"``) rounded rectangle filling the window.

    Returns ``None`` if the AppKit module doesn't expose ``NSView``
    (e.g. test stubs) — the caller falls back to a plain NSView and the
    user just doesn't see the rounded background, but logic still works.
    """
    cached = getattr(appkit, "_HeliosVoiceNoteBackgroundView", None)
    if cached is not None:
        return cached

    try:
        NSView = appkit.NSView
        # Verify the bezier path / color helpers exist before we commit
        # to subclassing — under MagicMock these all "exist" but the
        # subclass registration won't actually run, which is fine.
        _ = appkit.NSBezierPath
    except Exception:
        return None

    # Resolve NSMakeRect from Foundation (preferred) or AppKit (fallback,
    # since PyObjC re-exports it). Captured at class-build time so the
    # closure doesn't have to look it up on every redraw.
    ns_make_rect = None
    if foundation is not None:
        ns_make_rect = getattr(foundation, "NSMakeRect", None)
    if ns_make_rect is None:
        ns_make_rect = getattr(appkit, "NSMakeRect", None)

    try:
        class HeliosVoiceNoteBackgroundView(NSView):  # type: ignore[misc]
            def _set_indicator(self, indicator):  # noqa: D401
                """Stash a back-reference to the Python indicator object."""
                self._indicator = indicator

            def isOpaque(self):  # noqa: N802 - ObjC method name
                return False

            def drawRect_(self, _dirty):  # noqa: N802 - ObjC method name
                indicator = getattr(self, "_indicator", None)
                color_state = "red"
                if indicator is not None:
                    color_state = getattr(indicator, "_color_state", "red")
                bounds = self.bounds()
                # Inset slightly so the rounded edges don't get clipped
                # by the window's bounds.
                inset = 1.0
                if ns_make_rect is not None:
                    rect = ns_make_rect(
                        inset,
                        inset,
                        bounds.size.width - 2 * inset,
                        bounds.size.height - 2 * inset,
                    )
                else:  # pragma: no cover - defensive; AppKit always has it
                    rect = bounds
                radius = 12.0
                path = appkit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    rect, radius, radius
                )
                if color_state == "amber":
                    # macOS systemOrange-ish.
                    fill = appkit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.95, 0.62, 0.07, 1.0
                    )
                else:
                    # macOS systemRed-ish.
                    fill = appkit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        0.86, 0.18, 0.18, 1.0
                    )
                fill.setFill()
                path.fill()
    except Exception:
        return None

    setattr(appkit, "_HeliosVoiceNoteBackgroundView", HeliosVoiceNoteBackgroundView)
    return HeliosVoiceNoteBackgroundView


class VoiceNoteIndicator:
    """Floating recording indicator window.

    Parameters
    ----------
    client:
        Synchronous :class:`helios.menubar.client.HeliosClient`. Used to
        poll ``/v1/voice-note/active`` and to send the stop RPC when the
        user clicks Stop.
    config:
        Top-level :class:`helios.config.HeliosConfig`. Read for the
        initial window position.
    persist_position:
        Optional callable invoked with ``{"x": int, "y": int}`` whenever
        the user finishes dragging the window. The menu bar app supplies
        a callable that writes back to capture.toml; tests inject a
        spy. ``None`` ⇒ position changes are not persisted (useful for
        ad-hoc testing).
    """

    def __init__(
        self,
        client: Any,
        config: Any,
        persist_position: Callable[[dict[str, int]], None] | None = None,
        stop_callback: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._persist_position = persist_position
        # When set, the Stop NSButton dispatches here instead of calling
        # the daemon's stop endpoint directly. The menu bar app passes its
        # own ``_trigger_voice_note_stop`` so stop-from-indicator and
        # stop-from-menu both flow through the same path that opens the
        # save window with the response. Without this, the indicator's
        # stop fires the RPC but no UI ever opens the save window.
        self._stop_callback = stop_callback

        # Mutable state observable by tests without poking PyObjC.
        self._visible: bool = False
        self._approaching_cap: bool = False
        self._color_state: str = "red"  # "red" | "amber"
        self._elapsed_seconds: float = 0.0
        self._current_rms: float = 0.0
        self._last_position: dict[str, int] | None = None

        # PyObjC handles. Populated lazily on first :meth:`show`.
        self._appkit: _PyObjCImports | None = None
        self._window: Any = None
        self._content_view: Any = None
        self._poll_timer: Any = None
        self._pulse_timer: Any = None
        # Live widgets the poll handler updates each tick.
        self._time_label: Any = None
        self._level_bar: Any = None
        self._stop_button: Any = None
        # NSObject-backed click bridge (Bug 2 fix). Held strongly so
        # AppKit's weak ref to the target stays valid for the window's
        # lifetime.
        self._stop_button_target: Any = None
        # NSObject-backed timer targets (cycle 3 fix — NSTimer requires
        # an NSObject target; the previous self-as-target pattern silently
        # no-op'd every fire, leaving the timer label frozen at 00:00).
        self._poll_target: Any = None
        self._pulse_target: Any = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def show(self) -> None:
        """Create (or re-show) the floating indicator window.

        Idempotent: a second call while visible just brings the existing
        window forward. Raises :class:`RuntimeError` on non-mac when
        PyObjC isn't available.
        """
        if self._visible and self._window is not None:
            # Already visible — bring forward and return.
            try:
                self._window.makeKeyAndOrderFront_(None)
            except Exception:  # pragma: no cover - defensive
                pass
            return

        if self._appkit is None:
            self._appkit = _import_appkit()
        ak = self._appkit.AppKit

        # Resolve initial position from config.
        position_cfg = getattr(
            self._config.voice_note.indicator,
            "floating_pill_position",
            "top_right",
        )
        # Allow runtime override via ``last_position`` if present.
        last_pos_cfg = getattr(
            self._config.voice_note.indicator, "last_position", None
        )
        position = last_pos_cfg if last_pos_cfg else position_cfg

        screen_size = self._primary_screen_size()
        x, y = _resolve_position(position, screen_size)
        self._last_position = {"x": x, "y": y}

        rect = self._appkit.Foundation.NSMakeRect(
            x, y, _INDICATOR_WIDTH, _INDICATOR_HEIGHT
        )
        style_mask = ak.NSWindowStyleMaskBorderless
        backing = ak.NSBackingStoreBuffered
        window = ak.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style_mask, backing, False
        )
        window.setLevel_(ak.NSStatusWindowLevel)
        # The window itself is transparent — the rounded red rect is
        # drawn by ``_IndicatorBackgroundView`` so we can have rounded
        # corners on a borderless window. ``setOpaque_(False)`` is
        # required for the corners outside the rect to not paint black.
        try:
            window.setBackgroundColor_(ak.NSColor.clearColor())
        except Exception:  # pragma: no cover - defensive
            pass
        window.setOpaque_(False)
        try:
            window.setHasShadow_(True)
        except Exception:  # pragma: no cover - defensive
            pass
        window.setIgnoresMouseEvents_(False)
        window.setMovableByWindowBackground_(True)
        window.setReleasedWhenClosed_(False)

        self._window = window
        self._build_content_view()
        window.makeKeyAndOrderFront_(None)

        self._visible = True
        self._start_polling()
        self._start_pulse()
        _log.info("voice_note_indicator_shown", x=x, y=y)

    def hide(self) -> None:
        """Hide the window without tearing down PyObjC handles."""
        if self._window is not None:
            try:
                self._window.orderOut_(None)
            except Exception:  # pragma: no cover - defensive
                pass
        self._visible = False

    def dismiss(self) -> None:
        """Hide the window and stop all timers (poll + pulse)."""
        self._stop_polling()
        self._stop_pulse()
        self.hide()
        # Close to release the NSWindow's retain. Subsequent ``show()``
        # rebuilds from scratch.
        if self._window is not None:
            try:
                self._window.close()
            except Exception:  # pragma: no cover - defensive
                pass
        self._window = None
        self._content_view = None
        _log.info("voice_note_indicator_dismissed")

    # ------------------------------------------------------------------
    # Test-visible state accessors (tests assert against these instead
    # of pixel-rendering, since the test environment mocks PyObjC).
    # ------------------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def color_state(self) -> str:
        """``"red"`` normally, ``"amber"`` when ``approaching_cap=true``."""
        return self._color_state

    @property
    def elapsed_seconds(self) -> float:
        return self._elapsed_seconds

    @property
    def current_rms(self) -> float:
        return self._current_rms

    @property
    def approaching_cap(self) -> bool:
        return self._approaching_cap

    @property
    def last_position(self) -> dict[str, int] | None:
        return self._last_position

    # ------------------------------------------------------------------
    # Internals — content view
    # ------------------------------------------------------------------

    def _build_content_view(self) -> None:
        """Create the NSView holding the dot/timer/level/stop button.

        The content view is a custom NSView subclass whose ``drawRect_``
        paints a solid red (or amber, when ``approaching_cap``) rounded
        rectangle filling the window. On top of that we add:

        * A static red dot indicator on the left.
        * An NSTextField showing the elapsed MM:SS time.
        * A thin level-meter rect bound to ``current_rms``.
        * An NSButton wired to :meth:`handle_stop_clicked` via a real
          NSObject-backed bridge so PyObjC's selector dispatch actually
          fires (Bug 2 fix).
        """
        ak = self._appkit.AppKit
        Foundation = self._appkit.Foundation
        rect = Foundation.NSMakeRect(
            0, 0, _INDICATOR_WIDTH, _INDICATOR_HEIGHT
        )

        # Custom subclass with drawRect_ for the rounded red/amber bg.
        bg_class = _make_indicator_background_class(ak, Foundation)
        if bg_class is not None:
            try:
                view = bg_class.alloc().initWithFrame_(rect)
                # Hand the view a back-reference so drawRect_ can read
                # ``color_state`` directly without us threading through
                # additional state.
                try:
                    view._set_indicator(self)  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover - defensive
                    pass
            except Exception:  # pragma: no cover - defensive
                view = ak.NSView.alloc().initWithFrame_(rect)
        else:
            view = ak.NSView.alloc().initWithFrame_(rect)

        self._window.setContentView_(view)
        self._content_view = view

        # Static red dot on the left edge. ~10x10 px circle (drawn as a
        # filled NSBox with rounded corners — simpler than a custom
        # NSView). Falls back to skipping silently on stub AppKit.
        try:
            dot_rect = Foundation.NSMakeRect(14, 25, 10, 10)
            dot = ak.NSBox.alloc().initWithFrame_(dot_rect)
            try:
                dot.setBoxType_(4)  # NSBoxCustom
            except Exception:  # pragma: no cover - older AppKit
                pass
            try:
                dot.setBorderType_(0)  # NSNoBorder
            except Exception:  # pragma: no cover
                pass
            try:
                dot.setFillColor_(ak.NSColor.whiteColor())
            except Exception:  # pragma: no cover - older AppKit
                pass
            try:
                # Fully rounded => circle.
                dot.setCornerRadius_(5.0)
            except Exception:  # pragma: no cover
                pass
            try:
                dot.setTitlePosition_(0)  # NSNoTitle
            except Exception:  # pragma: no cover
                pass
            view.addSubview_(dot)
        except Exception:  # pragma: no cover - defensive
            pass

        # Elapsed MM:SS label.
        try:
            label_rect = Foundation.NSMakeRect(32, 18, 70, 26)
            label = ak.NSTextField.alloc().initWithFrame_(label_rect)
            label.setStringValue_(self._format_elapsed(self._elapsed_seconds))
            label.setBezeled_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(False)
            try:
                label.setTextColor_(ak.NSColor.whiteColor())
            except Exception:  # pragma: no cover
                pass
            try:
                label.setFont_(ak.NSFont.boldSystemFontOfSize_(14))
            except Exception:  # pragma: no cover
                pass
            view.addSubview_(label)
            self._time_label = label
        except Exception:  # pragma: no cover - defensive
            self._time_label = None

        # Level bar — thin filled rect to the right of the label whose
        # width tracks ``current_rms``. Implemented as an NSBox so we
        # get a colored fill cheaply. Tests don't assert on it.
        try:
            level_rect = Foundation.NSMakeRect(108, 26, 1, 8)
            level = ak.NSBox.alloc().initWithFrame_(level_rect)
            try:
                level.setBoxType_(4)  # NSBoxCustom
            except Exception:  # pragma: no cover
                pass
            try:
                level.setBorderType_(0)
            except Exception:  # pragma: no cover
                pass
            try:
                level.setFillColor_(ak.NSColor.whiteColor())
            except Exception:  # pragma: no cover
                pass
            try:
                level.setCornerRadius_(2.0)
            except Exception:  # pragma: no cover
                pass
            try:
                level.setTitlePosition_(0)
            except Exception:  # pragma: no cover
                pass
            view.addSubview_(level)
            self._level_bar = level
        except Exception:  # pragma: no cover - defensive
            self._level_bar = None

        # Stop button — NSButton on the right edge with a real NSObject
        # click target.
        try:
            btn_rect = Foundation.NSMakeRect(
                _INDICATOR_WIDTH - 60, 14, 50, 32
            )
            button = ak.NSButton.alloc().initWithFrame_(btn_rect)
            button.setTitle_("Stop")
            try:
                button.setBezelStyle_(ak.NSBezelStyleRounded)
            except Exception:  # pragma: no cover
                pass

            target = self._build_stop_button_target()
            if target is not None:
                button.setTarget_(target)
                button.setAction_("doAction:")
                self._stop_button_target = target  # keep alive
            view.addSubview_(button)
            self._stop_button = button
        except Exception:  # pragma: no cover - defensive
            self._stop_button = None

    def _build_stop_button_target(self) -> Any:
        """Construct an NSObject-backed bridge for the stop button.

        AppKit dispatches button actions via ``[target action:sender]``.
        The target must be an NSObject subclass — a plain Python object
        won't work (the previous code passed ``self`` directly, which is
        why clicks fired nothing per Bug 2). We build a tiny subclass
        whose ``-doAction:`` selector calls back into Python.
        """
        ak = self._appkit.AppKit
        try:
            import objc  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            cls = _make_stop_button_target_class(ak, objc)
        except Exception:  # pragma: no cover - defensive
            return None

        def _bridge() -> None:
            self.handle_stop_clicked()

        try:
            instance = cls.alloc().initWithBridge_(_bridge)
            return instance
        except Exception:  # pragma: no cover - test stubs may not support this
            return None

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        s = max(0, int(seconds))
        return f"{s // 60:02d}:{s % 60:02d}"

    def _primary_screen_size(self) -> tuple[int, int]:
        """Return the primary display's pixel size (w, h)."""
        ak = self._appkit.AppKit
        try:
            screen = ak.NSScreen.mainScreen()
            if screen is None:
                return 1440, 900
            frame = screen.frame()
            return int(frame.size.width), int(frame.size.height)
        except Exception:  # pragma: no cover - defensive
            return 1440, 900

    # ------------------------------------------------------------------
    # Internals — polling
    # ------------------------------------------------------------------

    def _start_polling(self) -> None:
        ak = self._appkit.AppKit
        objc = self._appkit.objc
        if self._poll_timer is not None:
            return
        # NSTimer requires an NSObject target. Build a small bridge
        # that calls back into ``handle_poll_tick``.
        try:
            target_cls = _make_timer_target_class(ak, objc)
            self._poll_target = target_cls.alloc().initWithBridge_(
                self.handle_poll_tick
            )
            self._poll_timer = ak.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                _POLL_INTERVAL_SECONDS,
                self._poll_target,
                "timerFired:",
                None,
                True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("voice_note_indicator_poll_timer_failed", error=str(exc))
            self._poll_timer = None
            self._poll_target = None

    def _stop_polling(self) -> None:
        if self._poll_timer is not None:
            try:
                self._poll_timer.invalidate()
            except Exception:  # pragma: no cover - defensive
                pass
        self._poll_timer = None
        self._poll_target = None

    def _start_pulse(self) -> None:
        """Kick the dot-pulse animation. Implemented as a 500ms NSTimer
        that flips a state flag; the redraw is handled by the content
        view's drawRect_ in the production build (here we just track
        the flag for unit-test visibility)."""
        ak = self._appkit.AppKit
        objc = self._appkit.objc
        if self._pulse_timer is not None:
            return
        try:
            target_cls = _make_timer_target_class(ak, objc)
            self._pulse_target = target_cls.alloc().initWithBridge_(
                self.handle_pulse_tick
            )
            self._pulse_timer = ak.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.5,
                self._pulse_target,
                "timerFired:",
                None,
                True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("voice_note_indicator_pulse_timer_failed", error=str(exc))
            self._pulse_timer = None
            self._pulse_target = None

    def _stop_pulse(self) -> None:
        if self._pulse_timer is not None:
            try:
                self._pulse_timer.invalidate()
            except Exception:  # pragma: no cover - defensive
                pass
        self._pulse_timer = None
        self._pulse_target = None

    # ------------------------------------------------------------------
    # Selector handlers (also directly callable by unit tests)
    # ------------------------------------------------------------------

    def handle_poll_tick(self) -> None:
        """Single poll cycle. Invoked by :meth:`_on_poll_tick` and tests.

        Reads ``/v1/voice-note/active`` and updates state. Dismisses on
        ``active=null`` or any client error (graceful disappear).
        """
        # Lazy-import the exception types so this module imports cleanly
        # on non-mac hosts (where ``helios.menubar.client`` itself imports
        # OK — it's pure httpx — but the user might still want to import
        # this module without instantiating).
        from helios.menubar.client import (
            DaemonAuthFailed,
            DaemonTimeout,
            DaemonUnreachable,
        )

        try:
            resp = self._client.get_active_voice_note()
        except (DaemonUnreachable, DaemonTimeout, DaemonAuthFailed) as exc:
            _log.info(
                "voice_note_indicator_daemon_unreachable",
                error_type=type(exc).__name__,
            )
            self.dismiss()
            return
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning(
                "voice_note_indicator_poll_failed",
                error=str(exc),
            )
            self.dismiss()
            return

        active = resp.active
        if active is None:
            # Voice note ended (stop or cancel) — close the indicator.
            self.dismiss()
            return

        self._elapsed_seconds = float(active.elapsed_seconds)
        self._current_rms = float(active.current_rms)
        self._approaching_cap = bool(active.approaching_cap)
        self._color_state = "amber" if self._approaching_cap else "red"

        # Push live state into the visible widgets.
        if self._time_label is not None:
            try:
                self._time_label.setStringValue_(
                    self._format_elapsed(self._elapsed_seconds)
                )
                # Force a redraw — on a borderless/transparent window
                # NSTextField's auto-mark-dirty isn't always honored.
                self._time_label.setNeedsDisplay_(True)
            except Exception:  # pragma: no cover - defensive
                pass
        if self._level_bar is not None and self._appkit is not None:
            try:
                # Map RMS [0..1] to bar width [1..100] px (left-anchored
                # at x=108). Clamp so the bar always shows at least 1 px
                # of presence and never overflows the window.
                rms = max(0.0, min(1.0, float(self._current_rms)))
                width = 1 + int(rms * 99)
                Foundation = self._appkit.Foundation
                self._level_bar.setFrame_(
                    Foundation.NSMakeRect(108, 26, float(width), 8.0)
                )
            except Exception:  # pragma: no cover - defensive
                pass

        # Trigger a redraw on the content view if PyObjC is live.
        if self._content_view is not None:
            try:
                self._content_view.setNeedsDisplay_(True)
            except Exception:  # pragma: no cover - defensive
                pass

    def _on_poll_tick(self, _timer: Any) -> None:
        self.handle_poll_tick()

    def _on_pulse_tick(self, _timer: Any) -> None:
        self.handle_pulse_tick()

    def handle_pulse_tick(self) -> None:
        """Bridged from the pulse NSTimer (cycle 3 fix).

        Trigger a redraw on the content view so the dot's pulse + the
        elapsed-time label refresh. Without this hook firing, the label
        only shows whatever value setStringValue_ injected on the last
        :meth:`handle_poll_tick` call — but the redraw never gets
        forced, so AppKit may coalesce updates and the user sees a
        frozen 00:00.
        """
        if self._content_view is not None:
            try:
                self._content_view.setNeedsDisplay_(True)
            except Exception:  # pragma: no cover - defensive
                pass
        # Also nudge the label directly — NSTextField.setStringValue_
        # marks the field dirty, but on a borderless transparent window
        # the redraw cycle can be lazy. Forcing setNeedsDisplay_ on
        # the label guarantees a refresh.
        if self._time_label is not None:
            try:
                self._time_label.setNeedsDisplay_(True)
            except Exception:  # pragma: no cover - defensive
                pass

    def handle_stop_clicked(self) -> None:
        """Wired to the Stop NSButton.

        When the menu bar app has provided a stop_callback (production
        wiring), dispatch there — that callback handles the daemon RPC
        AND opens the save window with the response. When no callback
        is set (tests, standalone use), fall back to calling the daemon
        directly. Either way, the indicator dismisses itself once the
        action has been initiated.
        """
        if self._stop_callback is not None:
            try:
                self._stop_callback()
            except Exception as exc:  # noqa: BLE001 — never block dismiss
                _log.warning(
                    "voice_note_indicator_stop_callback_failed",
                    error=str(exc),
                )
        else:
            try:
                self._client.post_voice_note_stop()
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning("voice_note_indicator_stop_failed", error=str(exc))
        self.dismiss()

    def _on_stop_clicked(self, _sender: Any) -> None:
        self.handle_stop_clicked()

    # ------------------------------------------------------------------
    # Drag-end persistence (called by the menu bar app's NSWindow
    # delegate — production wiring lives in Wave 2).
    # ------------------------------------------------------------------

    def handle_drag_end(self, x: int, y: int) -> None:
        """Persist a new window position after the user finishes dragging.

        Wave 2 wires up an NSWindow delegate that calls this from
        ``windowDidMove_``. Tests call this directly to verify the
        persistence callback fires with the new coords.
        """
        new_pos = {"x": int(x), "y": int(y)}
        self._last_position = new_pos
        if self._persist_position is not None:
            try:
                self._persist_position(new_pos)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "voice_note_indicator_persist_failed",
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Test convenience
    # ------------------------------------------------------------------

    def _force_visible_for_test(self) -> None:
        """Pretend ``show()`` succeeded without touching PyObjC.

        Used by unit tests that mock the client and don't have AppKit.
        """
        self._visible = True


__all__ = [
    "VoiceNoteIndicator",
    "_resolve_position",
]
