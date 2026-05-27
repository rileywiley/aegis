"""Tests for the voice-note global hotkey listener (Track 4F.1).

Carbon + ApplicationServices are mocked via monkeypatch so the suite
runs on any platform. ``VirtualClock`` from the shared conftest drives
the deterministic 5-minute retry loop without real sleeping.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from helios.menubar import hotkey as hotkey_mod
from helios.menubar.hotkey import (
    CMD_KEY,
    CONTROL_KEY,
    OPTION_KEY,
    SHIFT_KEY,
    VoiceNoteHotkey,
    parse_combo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_carbon_bundle() -> Any:
    """Return a ``_CarbonImports``-shaped MagicMock pair.

    ``Carbon`` and ``ApplicationServices`` are MagicMocks so any
    attribute access (constants, factories, register/unregister calls)
    returns another MagicMock. Tests assert on call signatures and
    side-effects, not real Carbon behaviour.
    """
    bundle = hotkey_mod._CarbonImports()
    carbon = MagicMock(name="Carbon")
    app_services = MagicMock(name="ApplicationServices")

    # The InstallEventHandler call returns a fake handler ref we can
    # later assert was passed back to RemoveEventHandler.
    carbon.InstallEventHandler.return_value = "FAKE_HANDLER_REF"
    carbon.RegisterEventHotKey.return_value = "FAKE_HOTKEY_REF"

    # Constants accessed by the production code.
    carbon.kEventClassKeyboard = 0x6B626420
    carbon.kEventHotKeyPressed = 5

    bundle.Carbon = carbon
    bundle.ApplicationServices = app_services
    return bundle


def _patch_carbon_granted(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ``_import_carbon`` to return a stub and accessibility = True."""
    bundle = _make_carbon_bundle()
    bundle.ApplicationServices.AXIsProcessTrusted.return_value = True
    monkeypatch.setattr(hotkey_mod, "_import_carbon", lambda: bundle)
    return bundle


def _patch_carbon_denied(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ``_import_carbon`` and force AXIsProcessTrusted=False."""
    bundle = _make_carbon_bundle()
    bundle.ApplicationServices.AXIsProcessTrusted.return_value = False
    monkeypatch.setattr(hotkey_mod, "_import_carbon", lambda: bundle)
    return bundle


# ---------------------------------------------------------------------------
# Combo parsing — pure-function tests (no fixtures needed)
# ---------------------------------------------------------------------------


def test_parse_combo_alt_cmd_v():
    """alt+cmd+v → (optionKey | cmdKey, 9)."""
    mods, kc = parse_combo("alt+cmd+v")
    assert mods == OPTION_KEY | CMD_KEY
    assert kc == 9


def test_parse_combo_ctrl_shift_a():
    """ctrl+shift+a → (controlKey | shiftKey, 0)."""
    mods, kc = parse_combo("ctrl+shift+a")
    assert mods == CONTROL_KEY | SHIFT_KEY
    assert kc == 0


def test_parse_combo_opt_alias():
    """``opt`` is an alias for ``alt``."""
    mods_alt, _ = parse_combo("alt+cmd+v")
    mods_opt, _ = parse_combo("opt+cmd+v")
    assert mods_alt == mods_opt


def test_parse_combo_case_insensitive():
    mods, kc = parse_combo("ALT+CMD+V")
    assert mods == OPTION_KEY | CMD_KEY
    assert kc == 9


def test_parse_combo_function_key():
    mods, kc = parse_combo("ctrl+f1")
    assert mods == CONTROL_KEY
    assert kc == 122


def test_parse_combo_invalid_modifier():
    with pytest.raises(ValueError, match="unknown modifier"):
        parse_combo("super+v")


def test_parse_combo_invalid_key():
    with pytest.raises(ValueError, match="unknown key"):
        parse_combo("cmd+nope")


def test_parse_combo_empty_raises():
    with pytest.raises(ValueError):
        parse_combo("")


def test_parse_combo_no_modifier_raises():
    with pytest.raises(ValueError, match="at least one modifier"):
        parse_combo("v")


# ---------------------------------------------------------------------------
# 1) start() with accessibility granted → registers, on_trigger fires on press
# ---------------------------------------------------------------------------


async def test_start_registers_when_accessibility_granted(
    monkeypatch, virtual_clock
):
    bundle = _patch_carbon_granted(monkeypatch)

    fired: list[int] = []

    def _on_trigger() -> None:
        fired.append(1)

    hk = VoiceNoteHotkey("alt+cmd+v", _on_trigger, virtual_clock)
    await hk.start()

    assert hk.registered is True
    bundle.Carbon.RegisterEventHotKey.assert_called_once()
    args = bundle.Carbon.RegisterEventHotKey.call_args.args
    assert args[0] == 9  # keycode for v
    assert args[1] == OPTION_KEY | CMD_KEY  # modifiers

    # Simulate a hotkey press by invoking the installed callback directly.
    handler_call = bundle.Carbon.InstallEventHandler.call_args
    callback = handler_call.args[1]
    callback(None, None, None)

    assert fired == [1]

    await hk.stop()


# ---------------------------------------------------------------------------
# 2) start() without accessibility → registered stays False, retry loop spawned
# ---------------------------------------------------------------------------


async def test_start_without_accessibility_spawns_retry_loop(
    monkeypatch, virtual_clock
):
    _patch_carbon_denied(monkeypatch)

    hk = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk.start()

    assert hk.registered is False
    assert hk._retry_task is not None
    assert not hk._retry_task.done()

    await hk.stop()


# ---------------------------------------------------------------------------
# 3) Retry loop: clock advances 5 minutes, accessibility flips → registers
# ---------------------------------------------------------------------------


async def test_retry_loop_registers_when_permission_appears(
    monkeypatch, virtual_clock
):
    bundle = _make_carbon_bundle()
    bundle.ApplicationServices.AXIsProcessTrusted.return_value = False
    monkeypatch.setattr(hotkey_mod, "_import_carbon", lambda: bundle)

    hk = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk.start()
    assert hk.registered is False

    # Yield once so the retry task gets to call `clock.sleep` and
    # actually register itself with the VirtualClock's sleepers list.
    await asyncio.sleep(0)

    # Flip accessibility to granted, then advance the virtual clock past
    # the 5-minute retry interval.
    bundle.ApplicationServices.AXIsProcessTrusted.return_value = True
    await virtual_clock.advance(301)
    for _ in range(10):
        if hk.registered:
            break
        await asyncio.sleep(0)

    assert hk.registered is True
    bundle.Carbon.RegisterEventHotKey.assert_called_once()

    await hk.stop()


# ---------------------------------------------------------------------------
# 4) stop() unregisters and cancels retry — idempotent
# ---------------------------------------------------------------------------


async def test_stop_unregisters_and_is_idempotent(monkeypatch, virtual_clock):
    bundle = _patch_carbon_granted(monkeypatch)

    hk = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk.start()
    assert hk.registered is True

    await hk.stop()
    bundle.Carbon.UnregisterEventHotKey.assert_called_once_with("FAKE_HOTKEY_REF")
    bundle.Carbon.RemoveEventHandler.assert_called_once_with("FAKE_HANDLER_REF")
    assert hk.registered is False

    # Second call must not raise nor double-unregister.
    await hk.stop()
    assert bundle.Carbon.UnregisterEventHotKey.call_count == 1
    assert bundle.Carbon.RemoveEventHandler.call_count == 1


async def test_stop_cancels_pending_retry_task(monkeypatch, virtual_clock):
    _patch_carbon_denied(monkeypatch)

    hk = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk.start()
    retry_task = hk._retry_task
    assert retry_task is not None
    assert not retry_task.done()

    await hk.stop()
    assert retry_task.done()
    assert hk._retry_task is None


# ---------------------------------------------------------------------------
# 5/6) Combo parsing for the two canonical examples (also covered above)
# ---------------------------------------------------------------------------
# (covered by test_parse_combo_alt_cmd_v / test_parse_combo_ctrl_shift_a)


# ---------------------------------------------------------------------------
# 7) Invalid combo → ValueError at construction time
# ---------------------------------------------------------------------------


async def test_invalid_combo_raises_at_construction(virtual_clock):
    with pytest.raises(ValueError):
        VoiceNoteHotkey("not-a-combo", lambda: None, virtual_clock)


async def test_unknown_key_raises_at_construction(virtual_clock):
    with pytest.raises(ValueError):
        VoiceNoteHotkey("cmd+frobnicate", lambda: None, virtual_clock)


# ---------------------------------------------------------------------------
# 8) PyObjC Carbon ImportError → start() returns gracefully, registered=False
# ---------------------------------------------------------------------------


async def test_start_with_carbon_import_error_returns_gracefully(
    monkeypatch, virtual_clock
):
    def _broken_import() -> Any:
        raise RuntimeError("Carbon unavailable")

    monkeypatch.setattr(hotkey_mod, "_import_carbon", _broken_import)

    hk = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk.start()  # must not raise

    assert hk.registered is False
    # Stop must also be safe — no Carbon bundle was ever stored.
    await hk.stop()


# ---------------------------------------------------------------------------
# 9) on_trigger called when hotkey fires — callback exception swallowed
# ---------------------------------------------------------------------------


async def test_on_trigger_invoked_on_press(monkeypatch, virtual_clock):
    bundle = _patch_carbon_granted(monkeypatch)
    fired: list[int] = []

    def _on_trigger() -> None:
        fired.append(1)

    hk = VoiceNoteHotkey("alt+cmd+v", _on_trigger, virtual_clock)
    await hk.start()

    # Pull the installed callback and invoke it twice to verify each
    # press fires `on_trigger`.
    callback = bundle.Carbon.InstallEventHandler.call_args.args[1]
    callback(None, None, None)
    callback(None, None, None)

    assert fired == [1, 1]
    await hk.stop()


async def test_trigger_exception_is_swallowed(monkeypatch, virtual_clock):
    bundle = _patch_carbon_granted(monkeypatch)

    def _on_trigger() -> None:
        raise RuntimeError("boom")

    hk = VoiceNoteHotkey("alt+cmd+v", _on_trigger, virtual_clock)
    await hk.start()

    callback = bundle.Carbon.InstallEventHandler.call_args.args[1]
    # Must not propagate.
    callback(None, None, None)

    await hk.stop()


# ---------------------------------------------------------------------------
# 10) Combo change: stop() + new instance → old combo unregistered, new combo
# registered. The stop() side is owned by this module; the "new instance"
# half just verifies the second instance registers cleanly.
# ---------------------------------------------------------------------------


async def test_stop_then_new_instance_registers_new_combo(
    monkeypatch, virtual_clock
):
    bundle = _patch_carbon_granted(monkeypatch)

    hk1 = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk1.start()
    assert hk1.registered is True
    await hk1.stop()

    bundle.Carbon.UnregisterEventHotKey.assert_called_once()

    # Caller (the menu bar) creates a fresh instance with a new combo.
    hk2 = VoiceNoteHotkey("ctrl+shift+a", lambda: None, virtual_clock)
    await hk2.start()
    assert hk2.registered is True
    # Two registrations now in total.
    assert bundle.Carbon.RegisterEventHotKey.call_count == 2

    last_call_args = bundle.Carbon.RegisterEventHotKey.call_args.args
    assert last_call_args[0] == 0  # keycode for a
    assert last_call_args[1] == CONTROL_KEY | SHIFT_KEY

    await hk2.stop()


# ---------------------------------------------------------------------------
# Bonus: start() is idempotent (second call while registered is a no-op)
# ---------------------------------------------------------------------------


async def test_start_is_idempotent_when_already_registered(
    monkeypatch, virtual_clock
):
    bundle = _patch_carbon_granted(monkeypatch)
    hk = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk.start()
    await hk.start()  # second call — should NOT re-register
    assert bundle.Carbon.RegisterEventHotKey.call_count == 1
    await hk.stop()


# ---------------------------------------------------------------------------
# Bonus: registration failure (RegisterEventHotKey raises) → schedule retry
# ---------------------------------------------------------------------------


async def test_register_failure_schedules_retry(monkeypatch, virtual_clock):
    bundle = _patch_carbon_granted(monkeypatch)
    bundle.Carbon.RegisterEventHotKey.side_effect = RuntimeError("conflict")

    hk = VoiceNoteHotkey("alt+cmd+v", lambda: None, virtual_clock)
    await hk.start()

    assert hk.registered is False
    assert hk._retry_task is not None
    assert not hk._retry_task.done()
    await hk.stop()
