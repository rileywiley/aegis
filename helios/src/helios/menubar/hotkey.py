"""Voice-note global hotkey listener (Track 4F.2).

Per HELIOS.md §12.9 the menu bar registers a global hotkey via
`pyobjc-framework-Carbon` (`RegisterEventHotKey`) when
`voice_note.hotkey_enabled = true` and macOS Accessibility permission
is granted.

Behaviour
---------

* The hotkey is **toggle**: every press fires `on_trigger`. The caller
  (the menu bar app) decides start-vs-stop based on the current
  daemon-side voice-note state. This module is intentionally dumb — it
  watches for the keystroke and dispatches a single callback.
* If Accessibility permission is missing at startup, registration is
  silently deferred. A periodic re-check (every 5 minutes via the
  injected `Clock`) re-attempts registration once permission becomes
  available. Permission revocation at runtime is not detected here —
  the OS simply stops delivering events; the next periodic check will
  re-register if permission is re-granted.
* If the Carbon framework is unavailable at all (non-mac, missing
  PyObjC dependency) the listener logs a warning and sits idle. This
  matches the rest of the menu bar package which imports cleanly on
  CI Linux.

Architecture
------------

* ASYNC API. `start()` / `stop()` are coroutines so the caller (the
  daemon-side scheduler glue, or `app.py` via
  `asyncio.run_coroutine_threadsafe`) can await registration without
  blocking the rumps run-loop.
* The actual Carbon event handler runs on the main run-loop thread.
  When the hotkey fires, the handler invokes `on_trigger` directly. If
  the trigger needs to do async work (e.g. POST to the daemon) it is
  the caller's responsibility to bounce that onto an asyncio loop.
* PyObjC imports are **lazy** so the module imports cleanly on
  non-mac. `_import_carbon()` raises `RuntimeError` on failure; the
  caller catches and degrades gracefully.

Logging is PII-safe — only flags and counters.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from helios.clock import Clock
from helios.log import get_logger

_log = get_logger("menubar.hotkey")


# ---------------------------------------------------------------------------
# Carbon constants — duplicated here so we don't have to import Carbon at
# module load time. Values from <Carbon/HIToolbox/Events.h>.
# ---------------------------------------------------------------------------

CMD_KEY = 0x100
SHIFT_KEY = 0x200
OPTION_KEY = 0x800
CONTROL_KEY = 0x1000

# Re-attempt cadence when Accessibility permission is missing.
_RETRY_INTERVAL_SECONDS = 300  # 5 minutes


# US QWERTY virtual keycodes (kVK_ANSI_*). Hardcoded so we don't depend
# on Carbon at parse time.
_KEYCODES: dict[str, int] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "9": 25,
    "7": 26,
    "8": 28,
    "0": 29,
    "o": 31,
    "u": 32,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "k": 40,
    "n": 45,
    "m": 46,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}


_MODIFIERS: dict[str, int] = {
    "cmd": CMD_KEY,
    "command": CMD_KEY,
    "shift": SHIFT_KEY,
    "alt": OPTION_KEY,
    "opt": OPTION_KEY,
    "option": OPTION_KEY,
    "ctrl": CONTROL_KEY,
    "control": CONTROL_KEY,
}


def parse_combo(combo: str) -> tuple[int, int]:
    """Parse ``"alt+cmd+v"`` into ``(carbon_mods_mask, keycode)``.

    Tokens are case-insensitive and joined by ``+``. The last token is
    the key; preceding tokens are modifiers. Raises ``ValueError`` for
    empty input, unknown modifiers/keys, or a missing key.
    """
    if not combo or not isinstance(combo, str):
        raise ValueError(f"hotkey combo must be a non-empty string, got: {combo!r}")

    tokens = [t.strip().lower() for t in combo.split("+") if t.strip()]
    if not tokens:
        raise ValueError(f"hotkey combo has no tokens: {combo!r}")
    if len(tokens) < 2:
        raise ValueError(
            f"hotkey combo must include at least one modifier and one key: {combo!r}"
        )

    *mod_tokens, key_token = tokens

    mods_mask = 0
    for mod in mod_tokens:
        if mod not in _MODIFIERS:
            raise ValueError(f"unknown modifier {mod!r} in combo {combo!r}")
        mods_mask |= _MODIFIERS[mod]

    if key_token not in _KEYCODES:
        raise ValueError(f"unknown key {key_token!r} in combo {combo!r}")

    return mods_mask, _KEYCODES[key_token]


# ---------------------------------------------------------------------------
# Lazy PyObjC imports
# ---------------------------------------------------------------------------


class _CarbonImports:
    """Holds the Carbon / ApplicationServices handles."""

    Carbon: Any = None
    ApplicationServices: Any = None


def _import_carbon() -> _CarbonImports:
    """Lazy-import Carbon + ApplicationServices.

    Raises ``RuntimeError`` if either framework is unavailable so
    callers can log and degrade. Production callers swallow the error
    and leave the listener idle.
    """
    try:
        import Carbon  # type: ignore[import-not-found]
        import ApplicationServices  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PyObjC Carbon / ApplicationServices not available — "
            "VoiceNoteHotkey requires macOS"
        ) from exc

    bundle = _CarbonImports()
    bundle.Carbon = Carbon
    bundle.ApplicationServices = ApplicationServices
    return bundle


def _is_accessibility_granted(carbon: _CarbonImports) -> bool:
    """Wrapper around ``AXIsProcessTrusted()``.

    Defined as a free function so tests can monkeypatch it without
    instantiating ``VoiceNoteHotkey``.
    """
    return bool(carbon.ApplicationServices.AXIsProcessTrusted())


# ---------------------------------------------------------------------------
# VoiceNoteHotkey
# ---------------------------------------------------------------------------


class VoiceNoteHotkey:
    """Carbon ``RegisterEventHotKey`` wrapper.

    Watches for the configured hotkey and calls ``on_trigger`` on each
    press. Auto-retries registration every 5 minutes if Accessibility
    permission isn't granted yet.
    """

    def __init__(
        self,
        combo: str,
        on_trigger: Callable[[], None],
        clock: Clock,
    ) -> None:
        # Validate the combo at construction time so configuration
        # errors surface immediately rather than 5 minutes later in the
        # retry loop.
        self._combo_str = combo
        self._mods_mask, self._keycode = parse_combo(combo)
        self._on_trigger = on_trigger
        self._clock = clock

        self._registered = False
        self._retry_task: asyncio.Task[None] | None = None
        self._stopped = False

        # Carbon handles populated on successful registration.
        self._carbon: _CarbonImports | None = None
        self._hotkey_ref: Any = None  # EventHotKeyRef
        self._event_handler_ref: Any = None  # EventHandlerRef

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def registered(self) -> bool:
        return self._registered

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Attempt to register the hotkey.

        If Accessibility permission isn't granted, spawn a background
        retry loop and return. Returns immediately on the happy path
        too — registration is synchronous in Carbon, but we keep the
        method async to match the daemon-bound API style.
        """
        if self._stopped:
            _log.warning("hotkey_start_after_stop")
            return
        if self._registered:
            return

        try:
            carbon = _import_carbon()
        except RuntimeError as exc:
            _log.warning(
                "hotkey_carbon_unavailable",
                error=str(exc),
                combo=self._combo_str,
            )
            return

        if not _is_accessibility_granted(carbon):
            _log.warning(
                "hotkey_accessibility_missing",
                combo=self._combo_str,
            )
            self._spawn_retry_loop(carbon)
            return

        ok = self._do_register(carbon)
        if not ok:
            # Registration failure (e.g. another app holds the combo).
            # Schedule a retry — the user might quit the conflicting app.
            self._spawn_retry_loop(carbon)

    async def stop(self) -> None:
        """Unregister the hotkey and cancel any pending retry task.

        Idempotent — calling twice does not raise.
        """
        self._stopped = True

        # Cancel pending retry task first so it doesn't race with us.
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
            try:
                await self._retry_task
            except (asyncio.CancelledError, Exception):
                # Swallow — cancellation and any background errors
                # shouldn't mask shutdown.
                pass
        self._retry_task = None

        if self._registered and self._carbon is not None:
            self._do_unregister()

    # ------------------------------------------------------------------
    # Registration internals
    # ------------------------------------------------------------------

    def _spawn_retry_loop(self, carbon: _CarbonImports) -> None:
        """Start the retry loop if one isn't already running."""
        if self._retry_task is not None and not self._retry_task.done():
            return
        self._retry_task = asyncio.create_task(self._retry_loop(carbon))

    async def _retry_loop(self, carbon: _CarbonImports) -> None:
        """Re-attempt registration every 5 minutes until it sticks."""
        while not self._registered and not self._stopped:
            try:
                await self._clock.sleep(_RETRY_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            if self._stopped:
                return
            if not _is_accessibility_granted(carbon):
                continue
            self._do_register(carbon)

    def _do_register(self, carbon: _CarbonImports) -> bool:
        """Perform the actual Carbon ``RegisterEventHotKey`` call.

        Returns True on success, False on failure. Any exception is
        logged and swallowed so a failed register doesn't crash the
        menu bar.
        """
        try:
            ref = carbon.Carbon.RegisterEventHotKey(
                self._keycode,
                self._mods_mask,
                # Hotkey ID — unique to this listener. We use a fixed
                # signature so multiple instances don't collide.
                self._make_hotkey_id(carbon),
                # Target (event target) — main run-loop event target.
                carbon.Carbon.GetApplicationEventTarget(),
                0,  # options
                None,  # outRef pointer placeholder; PyObjC returns it
            )
            handler = self._install_event_handler(carbon)
        except Exception as exc:  # noqa: BLE001 — never crash on register
            _log.warning(
                "hotkey_register_failed",
                error=str(exc),
                combo=self._combo_str,
            )
            return False

        self._carbon = carbon
        self._hotkey_ref = ref
        self._event_handler_ref = handler
        self._registered = True
        _log.info("hotkey_registered", combo=self._combo_str)
        return True

    def _do_unregister(self) -> None:
        """Tear down the hotkey + event handler.

        Best-effort — exceptions are logged and swallowed.
        """
        carbon = self._carbon
        if carbon is None:
            self._registered = False
            return

        try:
            if self._hotkey_ref is not None:
                carbon.Carbon.UnregisterEventHotKey(self._hotkey_ref)
        except Exception as exc:  # noqa: BLE001
            _log.warning("hotkey_unregister_failed", error=str(exc))
        try:
            if self._event_handler_ref is not None:
                carbon.Carbon.RemoveEventHandler(self._event_handler_ref)
        except Exception as exc:  # noqa: BLE001
            _log.warning("hotkey_remove_handler_failed", error=str(exc))

        self._hotkey_ref = None
        self._event_handler_ref = None
        self._registered = False

    def _make_hotkey_id(self, carbon: _CarbonImports) -> Any:
        """Build the ``EventHotKeyID`` struct.

        PyObjC exposes ``EventHotKeyID`` as a tuple-shaped record; for
        ease of testing we delegate to a Carbon factory if available
        and otherwise fall back to ``(signature, id)``.
        """
        factory = getattr(carbon.Carbon, "EventHotKeyID", None)
        if factory is not None:
            try:
                return factory(b"HELI", 1)
            except Exception:  # noqa: BLE001
                pass
        return (b"HELI", 1)

    def _install_event_handler(self, carbon: _CarbonImports) -> Any:
        """Install the kEventHotKeyPressed handler.

        The handler dispatches ``on_trigger`` on the main run-loop
        thread. Exceptions inside ``on_trigger`` are logged and
        swallowed — we never want an exception to escape to the
        Carbon event pump.
        """

        def _carbon_callback(_handler, _event, _user_data):
            self._fire_trigger()
            return 0  # noErr

        spec = (
            carbon.Carbon.kEventClassKeyboard,
            carbon.Carbon.kEventHotKeyPressed,
        )
        return carbon.Carbon.InstallEventHandler(
            carbon.Carbon.GetApplicationEventTarget(),
            _carbon_callback,
            1,
            [spec],
            None,
            None,
        )

    def _fire_trigger(self) -> None:
        """Invoke the user-supplied callback safely."""
        try:
            self._on_trigger()
        except Exception as exc:  # noqa: BLE001 — never crash the runloop
            _log.warning("hotkey_trigger_callback_failed", error=str(exc))
