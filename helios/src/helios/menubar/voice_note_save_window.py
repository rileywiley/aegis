"""Floating voice-note save window (Track 4H.1).

Per HELIOS.md §12.11 the menu bar app opens a floating save window
after ``/v1/voice-note/stop`` returns. The window:

* Displays the transcript (read-only).
* Shows suggested attachments fetched from Aegis
  (``POST /api/voice-notes/preview-attachments``).
* Lets the user toggle suggestions and add manual ones (which queries
  ``GET /api/search?q=...&types=person,workstream,ask``).
* Counts down to auto-save (default 10 s); any user interaction
  cancels the countdown.
* On Save, POSTs a :class:`VoiceNoteCreate`-shaped payload to
  ``POST /api/voice-notes``.
* On Discard, POSTs to Helios ``/v1/voice-note/cancel`` (best-effort)
  and never POSTs to Aegis.
* On window close (× / click-away), behaves like Save.

Architecture
------------

* SYNC. Lives on the rumps run-loop. NSWindow + NSTimer dispatch
  there.
* PyObjC is **lazy-imported** inside :meth:`show`. The module imports
  cleanly on non-mac (so the test suite runs on CI Linux). On mac
  without PyObjC, ``show()`` raises a clean ``RuntimeError``.
* The Aegis preview-attachments call runs in a background thread so
  the run-loop stays responsive while the entity resolver works.
  Results dispatch back to the main run-loop via
  ``performSelectorOnMainThread:`` (or directly on the indicator's
  state when running under unit tests, since tests drive the
  callbacks themselves).
* PII-safe logging — transcript content NEVER logged.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import httpx

from helios.log import get_logger

_log = get_logger("menubar.voice_note_save_window")


# Window dimensions per HELIOS.md §12.11. ~480×360 is comfortable for
# transcript + suggestions + buttons without dominating the screen.
_WINDOW_WIDTH = 480
_WINDOW_HEIGHT = 360

# Countdown tick: every 1 s the window updates the Save label
# ("Save (auto-save in 8s)" → 7 → 6 …). Default total is read from
# config.voice_note.save_window.auto_save_timeout_seconds (or
# config.voice_note.auto_save_timeout_seconds for older configs)
# at show-time and stored on ``_countdown_total``.
_COUNTDOWN_TICK_SECONDS = 1.0

# Aegis preview-attachments timeout. Keep generous because the entity
# resolver may run an LLM call. If it exceeds, the UI shows
# "Suggestions unavailable" but still lets the user save manually.
_PREVIEW_TIMEOUT_SECONDS = 8.0

# Aegis save POST timeout. Larger because Aegis may do attachment
# inserts + extraction-queue enqueue synchronously.
_SAVE_TIMEOUT_SECONDS = 15.0

# Aegis manual-search timeout. Snappy — picker UX expects sub-second
# results.
_SEARCH_TIMEOUT_SECONDS = 3.0

# Default auto-save timeout when config lacks an explicit value.
_DEFAULT_AUTO_SAVE_TIMEOUT_SECONDS = 10


class _PyObjCImports:
    """Holds lazy PyObjC handles. Populated by :func:`_import_appkit`."""

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
            "PyObjC (AppKit/Foundation) not available — "
            "VoiceNoteSaveWindow requires macOS"
        ) from exc
    bundle = _PyObjCImports()
    bundle.AppKit = AppKit
    bundle.Foundation = Foundation
    bundle.objc = objc
    return bundle


def _resolve_auto_save_timeout(config: Any) -> int:
    """Read the auto-save countdown from a HeliosConfig-shaped object.

    Tries ``voice_note.save_window.auto_save_timeout_seconds`` first
    (the per-spec sub-config); falls back to
    ``voice_note.auto_save_timeout_seconds`` (the existing top-level
    field); defaults to 10 if neither exists.
    """
    voice_note = getattr(config, "voice_note", None)
    if voice_note is None:
        return _DEFAULT_AUTO_SAVE_TIMEOUT_SECONDS
    save_window = getattr(voice_note, "save_window", None)
    if save_window is not None:
        val = getattr(
            save_window, "auto_save_timeout_seconds", None
        )
        if isinstance(val, int) and val > 0:
            return val
    val = getattr(voice_note, "auto_save_timeout_seconds", None)
    if isinstance(val, int) and val > 0:
        return val
    return _DEFAULT_AUTO_SAVE_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# State containers (used by tests to assert behaviour without PyObjC)
# ---------------------------------------------------------------------------


class Suggestion:
    """One suggested attachment row.

    Attributes
    ----------
    type:
        ``"person" | "workstream" | "ask"``.
    id:
        Aegis entity id.
    label:
        Display name (e.g. ``"Sarah Lin"``).
    snippet:
        Optional secondary text (e.g. match preview).
    confidence:
        Float 0–1 from Aegis's resolver. Manual additions get 1.0.
    selected:
        Whether the checkbox is currently checked.
    source:
        ``"suggested"`` for entries from preview-attachments,
        ``"manual"`` for entries the user added via the picker.
    """

    __slots__ = (
        "type",
        "id",
        "label",
        "snippet",
        "confidence",
        "selected",
        "source",
    )

    def __init__(
        self,
        type: str,
        id: int,
        label: str,
        snippet: str = "",
        confidence: float = 1.0,
        selected: bool = False,
        source: str = "suggested",
    ) -> None:
        self.type = type
        self.id = id
        self.label = label
        self.snippet = snippet
        self.confidence = confidence
        self.selected = selected
        self.source = source

    def to_payload(self) -> dict[str, Any]:
        """Convert to the JSON shape Aegis's ``VoiceNoteAttachmentInput`` expects.

        Aegis schema (aegis/web/routes/voice_notes.py)::

            class VoiceNoteAttachmentInput(BaseModel):
                target_type: Literal["person", "workstream", "ask"]
                target_id: int
                is_suggested: bool = False
                confirmed_at: datetime | None = None

        ``is_suggested`` is True when the row originated from the
        preview-attachments suggestions feed (i.e. ``source == "suggested"``)
        and the user kept it checked. ``confirmed_at`` is omitted (Aegis
        defaults it to None) — the confirmation timestamp is the create
        request time, which Aegis records via ``created`` on the
        attachment row.
        """
        return {
            "target_type": self.type,
            "target_id": self.id,
            "is_suggested": self.source == "suggested",
        }

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"Suggestion(type={self.type!r}, id={self.id}, "
            f"label={self.label!r}, selected={self.selected})"
        )


def _make_save_button_target_class(appkit: Any, objc: Any) -> Any:
    """Build (or reuse) the NSObject subclass used as save-window button target.

    AppKit calls ``[target action:sender]`` on the registered selector.
    PyObjC's :func:`objc.selector` lets us expose a Python callable to
    Cocoa, but the *target* itself must inherit from NSObject — Cocoa
    won't dispatch selectors to raw Python objects. Mirrors the pattern
    used in :func:`helios.menubar.onboarding._make_button_target_class`
    and :func:`helios.menubar.voice_note_indicator._make_stop_button_target_class`.

    The class is cached on the AppKit module so we don't re-register on
    every :meth:`VoiceNoteSaveWindow.show`.
    """
    cached = getattr(appkit, "_HeliosVoiceNoteSaveButtonTarget", None)
    if cached is not None:
        return cached

    try:
        NSObject = appkit.NSObject
    except Exception:
        return None

    class HeliosVoiceNoteSaveButtonTarget(NSObject):  # type: ignore[misc]
        def initWithBridge_(self, bridge):  # noqa: N802 - ObjC selector name
            self = objc.super(HeliosVoiceNoteSaveButtonTarget, self).init()
            if self is None:
                return None
            self._bridge = bridge
            return self

        def doAction_(self, _sender):  # noqa: N802 - ObjC selector name
            try:
                self._bridge()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "save_window_button_handler_failed",
                    error=str(exc),
                )

    setattr(
        appkit,
        "_HeliosVoiceNoteSaveButtonTarget",
        HeliosVoiceNoteSaveButtonTarget,
    )
    return HeliosVoiceNoteSaveButtonTarget


class VoiceNoteSaveWindow:
    """PyObjC NSWindow shown after voice-note stop.

    Displays the transcript, suggested attachments, save/discard
    buttons, and an auto-save countdown.

    Parameters
    ----------
    helios_client:
        Synchronous :class:`helios.menubar.client.HeliosClient`. Used
        for the discard path (``POST /v1/voice-note/cancel``).
    aegis_base_url:
        Base URL for Aegis, e.g. ``http://127.0.0.1:8000``. No bearer
        token — Aegis is unauthed on localhost.
    config:
        Top-level :class:`helios.config.HeliosConfig`. Read for the
        auto-save countdown.
    on_close:
        Optional callable invoked once when the window finishes
        closing (Save success, Discard, or auto-save). The menu bar
        app uses this to clear its own ``save_window`` reference.
    """

    def __init__(
        self,
        helios_client: Any,
        aegis_base_url: str,
        config: Any,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._client = helios_client
        self._aegis_base_url = aegis_base_url.rstrip("/")
        self._config = config
        self._on_close = on_close

        # ---- state surfaced for tests -------------------------------
        self._visible: bool = False
        self._voice_note_id: int | None = None
        self._helios_session_id: int | None = None
        self._transcript_text: str = ""
        self._started_at: float = 0.0
        self._ended_at: float = 0.0
        self._duration_seconds: float = 0.0
        self._triggered_by: str = "menu_bar"
        self._is_excerpt: bool = False

        # Suggestion list. Two phases:
        # 1. show() sets ``_suggestions = []`` and ``_loading = True``.
        # 2. preview-attachments callback fills _suggestions and clears
        #    _loading. On timeout/error, _loading=False and
        #    _suggestions_unavailable=True.
        self._suggestions: list[Suggestion] = []
        self._loading: bool = False
        self._suggestions_unavailable: bool = False

        # Countdown state. Cancelled by any user interaction. Tests
        # call :meth:`_tick_countdown` to advance it.
        self._countdown_total: int = _DEFAULT_AUTO_SAVE_TIMEOUT_SECONDS
        self._countdown_remaining: int = 0
        self._countdown_active: bool = False

        # Save/error flow.
        self._saved: bool = False
        self._save_error: str | None = None  # truncated, no PII
        self._last_save_payload: dict[str, Any] | None = None

        # PyObjC handles. Populated lazily on first :meth:`show`.
        self._appkit: _PyObjCImports | None = None
        self._window: Any = None
        self._content_view: Any = None
        self._countdown_timer: Any = None
        self._save_button: Any = None
        self._discard_button: Any = None
        self._add_attachment_button: Any = None
        self._header_label: Any = None
        self._transcript_view: Any = None
        self._suggestions_stack: Any = None
        self._suggestions_loading_label: Any = None

        # The HTTP client we use to talk to Aegis. Lazily constructed
        # so unit tests can inject one via ``set_http_client_for_test``.
        self._http: httpx.Client | None = None

        # Background thread handle for preview-attachments. Tests
        # bypass it by calling :meth:`_apply_suggestions_response`.
        self._preview_thread: threading.Thread | None = None

        # Strong refs to the NSObject button-target bridges. Without
        # holding these, AppKit's weak target reference would let the
        # bridge get garbage-collected and clicks would fire nothing
        # (mirrors the Bug 2 fix from the indicator window).
        self._button_targets: list[Any] = []

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def show(
        self,
        voice_note_id: int,
        helios_session_id: int,
        transcript_text: str,
        started_at: float,
        ended_at: float,
        duration_seconds: float,
        triggered_by: str,
        is_excerpt: bool,
    ) -> None:
        """Open the save window for a freshly-stopped voice note.

        Idempotent in the sense that calling show() on an already-
        visible window will overwrite its data (the menu-bar app
        should be calling ``hide()`` between consecutive notes).
        Raises :class:`RuntimeError` on non-mac when PyObjC is
        unavailable.
        """
        # Stash the voice-note metadata immediately — we want it set
        # even if PyObjC fails so tests / callers can inspect.
        self._voice_note_id = voice_note_id
        self._helios_session_id = helios_session_id
        self._transcript_text = transcript_text
        self._started_at = started_at
        self._ended_at = ended_at
        self._duration_seconds = duration_seconds
        self._triggered_by = triggered_by
        self._is_excerpt = is_excerpt

        self._suggestions = []
        self._loading = True
        self._suggestions_unavailable = False
        self._saved = False
        self._save_error = None
        self._last_save_payload = None

        # Resolve countdown total from config now (tests stub config).
        self._countdown_total = _resolve_auto_save_timeout(self._config)
        self._countdown_remaining = self._countdown_total

        if self._appkit is None:
            self._appkit = _import_appkit()
        ak = self._appkit.AppKit
        Foundation = self._appkit.Foundation

        rect = Foundation.NSMakeRect(
            0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT
        )
        style_mask = (
            ak.NSWindowStyleMaskTitled
            | ak.NSWindowStyleMaskClosable
        )
        backing = ak.NSBackingStoreBuffered
        window = ak.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style_mask, backing, False
        )
        window.setLevel_(ak.NSFloatingWindowLevel)
        window.setReleasedWhenClosed_(False)
        window.setTitle_(
            f"Voice note - {self._format_duration(duration_seconds)}"
        )
        window.center()

        self._window = window
        self._build_content_view()
        window.makeKeyAndOrderFront_(None)

        self._visible = True
        self._start_countdown()
        self._launch_preview_attachments()

        _log.info(
            "save_window_shown",
            voice_note_id=voice_note_id,
            duration_seconds=duration_seconds,
            triggered_by=triggered_by,
            is_excerpt=is_excerpt,
        )

    def hide(self) -> None:
        """Hide the window without tearing down state."""
        if self._window is not None:
            try:
                self._window.orderOut_(None)
            except Exception:  # pragma: no cover - defensive
                pass
        self._visible = False

    def dismiss(self) -> None:
        """Tear down the window completely. Idempotent."""
        self._stop_countdown()
        self.hide()
        if self._window is not None:
            try:
                self._window.close()
            except Exception:  # pragma: no cover - defensive
                pass
        self._window = None
        self._content_view = None
        self._save_button = None
        self._discard_button = None
        self._add_attachment_button = None
        self._header_label = None
        self._transcript_view = None
        self._suggestions_stack = None
        self._suggestions_loading_label = None
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "save_window_on_close_failed", error=type(exc).__name__
                )
        _log.info("save_window_dismissed")

    # ------------------------------------------------------------------
    # Test-visible state accessors
    # ------------------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def transcript_text(self) -> str:
        return self._transcript_text

    @property
    def suggestions(self) -> list[Suggestion]:
        return list(self._suggestions)

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def suggestions_unavailable(self) -> bool:
        return self._suggestions_unavailable

    @property
    def countdown_active(self) -> bool:
        return self._countdown_active

    @property
    def countdown_remaining(self) -> int:
        return self._countdown_remaining

    @property
    def save_error(self) -> str | None:
        return self._save_error

    @property
    def saved(self) -> bool:
        return self._saved

    @property
    def last_save_payload(self) -> dict[str, Any] | None:
        return self._last_save_payload

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def set_http_client_for_test(self, client: httpx.Client) -> None:
        """Inject an httpx Client (real, mocked, or MockTransport-backed)."""
        self._http = client

    def _ensure_http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=_PREVIEW_TIMEOUT_SECONDS)
        return self._http

    # ------------------------------------------------------------------
    # Internals — content view
    # ------------------------------------------------------------------

    def _build_content_view(self) -> None:
        """Create the NSView holding transcript / suggestions / buttons.

        Per HELIOS.md §12.11 the save window contains:

        * Header label ("Voice note · MM:SS").
        * NSScrollView with a read-only NSTextView for the transcript.
        * NSStackView (or simple subview list) for suggestion checkboxes.
        * "+ Add attachment" button.
        * Discard / Save buttons in the footer.

        Layout uses absolute frames; we don't need auto-layout for a
        small modal. The save button reference is held so the countdown
        timer can update its label live.
        """
        ak = self._appkit.AppKit
        Foundation = self._appkit.Foundation

        rect = Foundation.NSMakeRect(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT)
        view = ak.NSView.alloc().initWithFrame_(rect)
        self._window.setContentView_(view)
        self._content_view = view

        # Header label — top of window.
        try:
            header = ak.NSTextField.alloc().initWithFrame_(
                Foundation.NSMakeRect(
                    16, _WINDOW_HEIGHT - 36, _WINDOW_WIDTH - 32, 22
                )
            )
            header.setStringValue_(
                f"Voice note · {self._format_duration(self._duration_seconds)}"
            )
            header.setBezeled_(False)
            header.setDrawsBackground_(False)
            header.setEditable_(False)
            header.setSelectable_(False)
            try:
                header.setFont_(ak.NSFont.boldSystemFontOfSize_(14))
            except Exception:  # noqa: BLE001
                pass
            view.addSubview_(header)
            self._header_label = header
        except Exception as exc:  # noqa: BLE001
            _log.debug("save_window_header_build_failed", error=str(exc))
            self._header_label = None

        # Transcript display — NSScrollView wrapping a read-only NSTextView.
        try:
            scroll_rect = Foundation.NSMakeRect(
                16, _WINDOW_HEIGHT - 180, _WINDOW_WIDTH - 32, 130
            )
            scroll = ak.NSScrollView.alloc().initWithFrame_(scroll_rect)
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(2)  # NSBezelBorder
            text_view = ak.NSTextView.alloc().initWithFrame_(scroll_rect)
            text_view.setEditable_(False)
            text_view.setSelectable_(True)
            text_view.setString_(self._transcript_text)
            scroll.setDocumentView_(text_view)
            view.addSubview_(scroll)
            self._transcript_view = text_view
        except Exception as exc:  # noqa: BLE001
            _log.debug("save_window_transcript_build_failed", error=str(exc))
            self._transcript_view = None

        # Suggestions area placeholder — populated dynamically when
        # ``_apply_suggestions_response`` fires. Uses a vertical
        # NSStackView when available; on stub AppKits we fall back to
        # a plain NSView.
        try:
            stack_rect = Foundation.NSMakeRect(
                16, 64, _WINDOW_WIDTH - 32, _WINDOW_HEIGHT - 252
            )
            try:
                stack = ak.NSStackView.alloc().initWithFrame_(stack_rect)
                stack.setOrientation_(1)  # NSUserInterfaceLayoutOrientationVertical
                stack.setSpacing_(4)
            except Exception:  # noqa: BLE001 - older / mocked AppKit
                stack = ak.NSView.alloc().initWithFrame_(stack_rect)
            view.addSubview_(stack)
            self._suggestions_stack = stack
            # Initial loading label.
            loading = ak.NSTextField.alloc().initWithFrame_(
                Foundation.NSMakeRect(0, 0, _WINDOW_WIDTH - 32, 22)
            )
            loading.setStringValue_("Loading suggestions...")
            loading.setBezeled_(False)
            loading.setDrawsBackground_(False)
            loading.setEditable_(False)
            loading.setSelectable_(False)
            stack.addSubview_(loading)
            self._suggestions_loading_label = loading
        except Exception as exc:  # noqa: BLE001
            _log.debug("save_window_suggestions_build_failed", error=str(exc))
            self._suggestions_stack = None
            self._suggestions_loading_label = None

        # Footer buttons. From left to right:
        #   "+ Add attachment", "Discard", "Save (auto-save in Ns)".
        self._add_attachment_button = self._make_button(
            ak,
            Foundation,
            title="+ Add attachment",
            frame=Foundation.NSMakeRect(16, 16, 160, 32),
            handler=self.handle_add_attachment_clicked,
        )
        if self._add_attachment_button is not None:
            view.addSubview_(self._add_attachment_button)

        self._discard_button = self._make_button(
            ak,
            Foundation,
            title="Discard",
            frame=Foundation.NSMakeRect(_WINDOW_WIDTH - 300, 16, 90, 32),
            handler=self.handle_discard_clicked,
        )
        if self._discard_button is not None:
            view.addSubview_(self._discard_button)

        self._save_button = self._make_button(
            ak,
            Foundation,
            title=self._save_button_title(),
            frame=Foundation.NSMakeRect(_WINDOW_WIDTH - 200, 16, 184, 32),
            handler=self.handle_save_clicked,
        )
        if self._save_button is not None:
            view.addSubview_(self._save_button)

    def _make_button(
        self,
        ak: Any,
        Foundation: Any,
        *,
        title: str,
        frame: Any,
        handler: Callable[[], None],
    ) -> Any:
        """Construct an NSButton wired to a zero-arg Python handler.

        AppKit dispatches button actions via ``[target action:sender]``.
        The target must be an NSObject subclass — passing a plain
        Python object (which the previous implementation effectively did
        via ``action_callable.__self__``) means clicks fire nothing
        (this was Bug 3 from the §12.5 smoke test). We mirror the
        :func:`helios.menubar.onboarding._make_button_target_class`
        pattern: build an NSObject subclass whose ``-doAction:`` selector
        invokes the Python handler, then keep a strong reference so the
        bridge isn't garbage-collected while AppKit holds a weak ref.

        Returns ``None`` if AppKit modules don't allow button
        construction (e.g. test stubs that raise on ``setTarget_``).
        """
        try:
            btn = ak.NSButton.alloc().initWithFrame_(frame)
            btn.setTitle_(title)
            try:
                btn.setBezelStyle_(ak.NSBezelStyleRounded)
            except Exception:  # noqa: BLE001
                pass

            target = self._build_button_target(ak, handler)
            if target is not None:
                btn.setTarget_(target)
                btn.setAction_("doAction:")
                self._button_targets.append(target)  # keep alive
            return btn
        except Exception:  # pragma: no cover - defensive
            return None

    def _build_button_target(
        self, ak: Any, handler: Callable[[], None]
    ) -> Any:
        """Construct an NSObject-backed bridge for ``handler``.

        Returns ``None`` if PyObjC isn't available or the AppKit
        subclass registration fails (e.g. under test stubs that don't
        support real subclassing).
        """
        try:
            import objc  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            cls = _make_save_button_target_class(ak, objc)
        except Exception:  # pragma: no cover - defensive
            return None
        if cls is None:
            return None
        try:
            return cls.alloc().initWithBridge_(handler)
        except Exception:  # pragma: no cover - test stubs may not support this
            return None

    def _save_button_title(self) -> str:
        """Compute the Save button label.

        Includes ``(auto-save in Ns)`` while the countdown is active,
        ``Saving…`` during a save in flight, ``Retry`` after a failure,
        and just ``Save`` otherwise.
        """
        if self._save_error is not None:
            return "Retry"
        if self._countdown_active:
            return f"Save (auto-save in {self._countdown_remaining}s)"
        return "Save"

    def _refresh_save_button_label(self) -> None:
        if self._save_button is not None:
            try:
                self._save_button.setTitle_(self._save_button_title())
            except Exception:  # pragma: no cover - defensive
                pass

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = max(0, int(seconds))
        return f"{s // 60}:{s % 60:02d}"

    # ------------------------------------------------------------------
    # Internals — countdown timer
    # ------------------------------------------------------------------

    def _start_countdown(self) -> None:
        ak = self._appkit.AppKit if self._appkit is not None else None
        self._countdown_active = True
        self._countdown_remaining = self._countdown_total
        self._refresh_save_button_label()

        if ak is None:
            return
        if self._countdown_timer is not None:
            return
        try:
            # NSTimer's target/selector API needs an NSObject target —
            # passing ``self`` (a plain Python object) gets the timer
            # registered but the selector dispatch silently fails, so
            # auto-save never fires (observed during §12.5 smoke run on
            # 2026-05-12). The block-based API takes a Python callable
            # directly via PyObjC's block bridge and avoids the target
            # subclass dance the Save/Discard buttons need.
            self._countdown_timer = (
                ak.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                    _COUNTDOWN_TICK_SECONDS,
                    True,
                    lambda _timer: self.handle_countdown_tick(),
                )
            )
        except Exception:  # pragma: no cover - defensive
            self._countdown_timer = None

    def _stop_countdown(self) -> None:
        if self._countdown_timer is not None:
            try:
                self._countdown_timer.invalidate()
            except Exception:  # pragma: no cover - defensive
                pass
        self._countdown_timer = None
        self._countdown_active = False
        self._refresh_save_button_label()

    def _cancel_countdown(self) -> None:
        """Stop the countdown without firing auto-save (user engaged)."""
        if not self._countdown_active:
            return
        self._stop_countdown()
        _log.info(
            "save_window_countdown_cancelled",
            voice_note_id=self._voice_note_id,
            remaining_seconds=self._countdown_remaining,
        )

    def handle_countdown_tick(self) -> None:
        """One countdown tick. Tests call this directly."""
        if not self._countdown_active:
            return
        self._countdown_remaining -= 1
        if self._countdown_remaining <= 0:
            self._countdown_remaining = 0
            self._refresh_save_button_label()
            self._stop_countdown()
            self.handle_save_clicked()
            return
        self._refresh_save_button_label()

    def _on_countdown_tick(self, _timer: Any) -> None:
        self.handle_countdown_tick()

    # ------------------------------------------------------------------
    # Internals — preview-attachments fetch
    # ------------------------------------------------------------------

    def _launch_preview_attachments(self) -> None:
        """Kick off the background fetch. Tests can override by calling
        :meth:`_apply_suggestions_response` / :meth:`_apply_suggestions_error`
        directly.
        """
        # Run in a thread so the rumps run-loop stays responsive. The
        # request is short-lived; we don't bother with a thread pool.
        thread = threading.Thread(
            target=self._preview_attachments_worker,
            name="save_window_preview_attachments",
            daemon=True,
        )
        self._preview_thread = thread
        thread.start()

    def _preview_attachments_worker(self) -> None:
        url = f"{self._aegis_base_url}/api/voice-notes/preview-attachments"
        body = {
            "transcript_text": self._transcript_text,
            "occurred_at": self._started_at,
        }
        try:
            http = self._ensure_http()
            resp = http.post(
                url, json=body, timeout=_PREVIEW_TIMEOUT_SECONDS
            )
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.HTTPError,
        ) as exc:
            _log.warning(
                "save_window_preview_attachments_failed",
                error_type=type(exc).__name__,
                voice_note_id=self._voice_note_id,
            )
            self._apply_suggestions_error()
            return
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning(
                "save_window_preview_attachments_unexpected_error",
                error_type=type(exc).__name__,
            )
            self._apply_suggestions_error()
            return

        if resp.status_code >= 400:
            _log.warning(
                "save_window_preview_attachments_http_error",
                status=resp.status_code,
                voice_note_id=self._voice_note_id,
            )
            self._apply_suggestions_error()
            return

        try:
            payload = resp.json()
        except ValueError:
            self._apply_suggestions_error()
            return
        self._apply_suggestions_response(payload)

    def _apply_suggestions_response(self, payload: dict[str, Any]) -> None:
        """Convert an Aegis preview-attachments response into Suggestions.

        Aegis's ``EntityMatch`` (aegis/web/routes/voice_notes.py) uses
        ``target_type`` / ``target_id`` / ``label`` / ``confidence`` /
        ``span``. We accept the legacy ``type`` / ``id`` / ``display_name`` /
        ``match_text`` shape too so older fixtures keep working.

        Public for tests — they call this directly to bypass the
        background thread / mock httpx.
        """
        matches = payload.get("matches") or []
        suggestions: list[Suggestion] = []
        for m in matches:
            try:
                stype = m.get("target_type") or m["type"]
                raw_id = m.get("target_id")
                if raw_id is None:
                    raw_id = m["id"]
                sid = int(raw_id)
                label = (
                    m.get("label")
                    or m.get("display_name")
                    or "Unknown"
                )
                snippet = (
                    m.get("span")
                    or m.get("snippet")
                    or m.get("match_text")
                    or ""
                )
                conf = float(m.get("confidence", 0.5))
            except (KeyError, TypeError, ValueError):
                continue
            # Pre-select high-confidence suggestions per HELIOS.md §12.11
            # (the "☑ Sarah Lin" example was high-confidence).
            preselect = conf >= 0.8
            suggestions.append(
                Suggestion(
                    type=stype,
                    id=sid,
                    label=label,
                    snippet=snippet,
                    confidence=conf,
                    selected=preselect,
                    source="suggested",
                )
            )
        self._suggestions = suggestions
        self._loading = False
        self._suggestions_unavailable = False
        # Trigger a redraw if we have a content view.
        if self._content_view is not None:
            try:
                self._content_view.setNeedsDisplay_(True)
            except Exception:  # pragma: no cover - defensive
                pass
        _log.info(
            "save_window_preview_attachments_loaded",
            voice_note_id=self._voice_note_id,
            count=len(suggestions),
        )

    def _apply_suggestions_error(self) -> None:
        """Mark suggestions as unavailable; user can still save manually."""
        self._suggestions = []
        self._loading = False
        self._suggestions_unavailable = True
        if self._content_view is not None:
            try:
                self._content_view.setNeedsDisplay_(True)
            except Exception:  # pragma: no cover - defensive
                pass

    # ------------------------------------------------------------------
    # Handlers — user actions
    # ------------------------------------------------------------------

    def handle_checkbox_clicked(self, index: int) -> None:
        """Toggle a suggestion's ``selected`` flag and cancel countdown."""
        if 0 <= index < len(self._suggestions):
            self._suggestions[index].selected = not self._suggestions[
                index
            ].selected
        self._cancel_countdown()

    def _on_checkbox_clicked(self, sender: Any) -> None:  # pragma: no cover
        # The production NSButton's tag holds the suggestion index.
        try:
            idx = int(sender.tag())
        except Exception:
            idx = -1
        self.handle_checkbox_clicked(idx)

    def handle_add_attachment_clicked(self) -> None:
        """User clicked '+ Add attachment'. Opens the picker, cancels countdown."""
        self._cancel_countdown()
        # TODO(helios-phase-4-followup): build the NSPanel / popover
        # picker. The data plumbing — :meth:`search_attachments` and
        # :meth:`add_manual_attachment` — already works; what's missing
        # is the floating panel with a search NSTextField and a
        # results NSTableView. Manual smoke testing currently exercises
        # this path via the Python API only. Tracked in HELIOS_BUILD_PLAN
        # §4H.2.

    def _on_add_attachment_clicked(self, _sender: Any) -> None:  # pragma: no cover
        self.handle_add_attachment_clicked()

    def search_attachments(self, query: str) -> list[Suggestion]:
        """Query Aegis search endpoint for the manual picker.

        Returns Suggestion rows (unselected, source="manual"). On
        error returns an empty list — the picker shows "no results".
        """
        url = f"{self._aegis_base_url}/api/search"
        params = {"q": query, "types": "person,workstream,ask"}
        try:
            http = self._ensure_http()
            resp = http.get(
                url, params=params, timeout=_SEARCH_TIMEOUT_SECONDS
            )
        except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
            _log.warning(
                "save_window_search_failed",
                error_type=type(exc).__name__,
            )
            return []
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        results: list[Suggestion] = []
        rows = data if isinstance(data, list) else data.get("results", [])
        for row in rows:
            try:
                results.append(
                    Suggestion(
                        type=row["type"],
                        id=int(row["id"]),
                        label=row.get("label") or row.get("display_name") or "Unknown",
                        snippet=row.get("snippet") or "",
                        confidence=1.0,
                        selected=False,
                        source="manual",
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return results

    def add_manual_attachment(self, suggestion: Suggestion) -> None:
        """Append a manually-picked attachment to the suggestion list."""
        suggestion.selected = True
        suggestion.source = "manual"
        self._suggestions.append(suggestion)
        self._cancel_countdown()

    def handle_save_clicked(self) -> None:
        """Save button clicked (or auto-save fired). POST to Aegis."""
        if self._saved:
            # Already persisted — just dismiss.
            self.dismiss()
            return

        # Build payload from current state.
        payload = self._build_save_payload()
        self._last_save_payload = payload

        url = f"{self._aegis_base_url}/api/voice-notes"
        try:
            http = self._ensure_http()
            resp = http.post(url, json=payload, timeout=_SAVE_TIMEOUT_SECONDS)
        except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
            self._save_error = f"network: {type(exc).__name__}"
            self._refresh_save_button_label()
            _log.warning(
                "save_window_save_network_error",
                error_type=type(exc).__name__,
                voice_note_id=self._voice_note_id,
            )
            return

        if resp.status_code >= 500:
            self._save_error = f"aegis_5xx_{resp.status_code}"
            self._refresh_save_button_label()
            _log.warning(
                "save_window_save_5xx",
                status=resp.status_code,
                voice_note_id=self._voice_note_id,
            )
            return
        if resp.status_code >= 400:
            self._save_error = f"aegis_4xx_{resp.status_code}"
            self._refresh_save_button_label()
            _log.warning(
                "save_window_save_4xx",
                status=resp.status_code,
                voice_note_id=self._voice_note_id,
            )
            return

        # Success.
        self._saved = True
        self._save_error = None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        _log.info(
            "save_window_saved",
            voice_note_id=self._voice_note_id,
            aegis_id=data.get("voice_note_id"),
            extraction_status=data.get("extraction_status"),
        )
        self.dismiss()

    def _on_save_clicked(self, _sender: Any) -> None:
        self.handle_save_clicked()

    def handle_discard_clicked(self) -> None:
        """Discard button clicked. Best-effort cancel on Helios; no Aegis POST."""
        self._cancel_countdown()
        try:
            self._client.post_voice_note_cancel()
        except Exception as exc:  # noqa: BLE001 — best-effort
            # Cancel may 4xx if the note has already been finalized in
            # Helios — that's fine, log and move on.
            _log.info(
                "save_window_discard_cancel_failed",
                error_type=type(exc).__name__,
                voice_note_id=self._voice_note_id,
            )
        _log.info(
            "save_window_discarded", voice_note_id=self._voice_note_id
        )
        self.dismiss()

    def _on_discard_clicked(self, _sender: Any) -> None:  # pragma: no cover
        self.handle_discard_clicked()

    def handle_window_did_resign_key(self) -> None:
        """User clicked outside the window. Treat as auto-save.

        Wired up by Wave 2's NSWindow delegate's ``windowDidResignKey:``.
        """
        if not self._visible or self._saved:
            return
        # Click-away counts as Save (per HELIOS.md §12.11).
        self.handle_save_clicked()

    def handle_window_close_clicked(self) -> None:
        """User clicked the × close button. Treat as auto-save."""
        if not self._visible or self._saved:
            return
        self.handle_save_clicked()

    # ------------------------------------------------------------------
    # Internals — payload assembly
    # ------------------------------------------------------------------

    def _build_save_payload(self) -> dict[str, Any]:
        """Assemble the VoiceNoteCreate-shaped payload for Aegis.

        Matches ``aegis.web.routes.voice_notes.VoiceNoteCreateRequest``:
        only the documented top-level fields plus an ``attachments`` list
        of ``VoiceNoteAttachmentInput``-shaped dicts (keys: ``target_type``,
        ``target_id``, ``is_suggested``, optional ``confirmed_at``).
        """
        attachments = [s.to_payload() for s in self._suggestions if s.selected]
        return {
            "helios_voice_note_id": self._voice_note_id,
            "helios_session_id": self._helios_session_id,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "duration_seconds": self._duration_seconds,
            "transcript_text": self._transcript_text,
            "triggered_by": self._triggered_by,
            "is_excerpt": self._is_excerpt,
            "source_device": "mac",
            "attachments": attachments,
        }


__all__ = [
    "Suggestion",
    "VoiceNoteSaveWindow",
    "_resolve_auto_save_timeout",
]
