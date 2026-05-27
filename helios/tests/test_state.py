"""Tests for the daemon state machine.

Covers initial state, every transition called out in spec Task 2D.1,
listener semantics (sync/async/exceptions/idempotent subscribe), and the
component-error merge contract.
"""

from __future__ import annotations

import asyncio

import pytest

from helios.state import (
    ComponentError,
    DaemonState,
    DaemonStateMachine,
)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


async def test_initial_state_is_armed():
    sm = DaemonStateMachine()
    s = sm.current()
    assert s.mode == "armed"
    assert s.active_session_id is None
    assert s.paused_until is None
    assert s.aegis_unreachable is False
    assert s.component_errors == {}
    assert s.last_error is None


async def test_initial_state_can_be_overridden():
    initial = DaemonState(mode="not_running")
    sm = DaemonStateMachine(initial=initial)
    assert sm.current().mode == "not_running"


# ---------------------------------------------------------------------------
# Mode transitions
# ---------------------------------------------------------------------------


async def test_recording_armed_recording_cycle():
    sm = DaemonStateMachine()

    await sm.transition(mode="recording", active_session_id=1)
    s = sm.current()
    assert s.mode == "recording"
    assert s.active_session_id == 1

    await sm.transition(mode="armed", active_session_id=None)
    s = sm.current()
    assert s.mode == "armed"
    assert s.active_session_id is None

    await sm.transition(mode="recording", active_session_id=2)
    s = sm.current()
    assert s.mode == "recording"
    assert s.active_session_id == 2


async def test_pause_until_sets_paused():
    sm = DaemonStateMachine()
    target_ts = 1_700_000_000.0
    await sm.transition(mode="paused", paused_until=target_ts)
    s = sm.current()
    assert s.mode == "paused"
    assert s.paused_until == target_ts


async def test_pause_clears_cleanly():
    """Caller resumes by setting mode=armed and paused_until=None explicitly."""
    sm = DaemonStateMachine()
    await sm.transition(mode="paused", paused_until=1_700_000_000.0)
    await sm.transition(mode="armed", paused_until=None)
    s = sm.current()
    assert s.mode == "armed"
    assert s.paused_until is None


async def test_permission_revoked_yields_error_with_component():
    sm = DaemonStateMachine()
    err = ComponentError("mic", "permission_denied", 1_700_000_000.0)
    await sm.transition(
        mode="error",
        component_errors={"mic": err},
        last_error="mic permission revoked",
    )
    s = sm.current()
    assert s.mode == "error"
    assert s.component_errors["mic"].message == "permission_denied"
    assert s.component_errors["mic"].component == "mic"
    assert s.last_error == "mic permission revoked"


async def test_component_failure_records_distinct_component():
    sm = DaemonStateMachine()
    err = ComponentError("system_audio", "device_lost", 1_700_000_001.0)
    await sm.transition(
        mode="error",
        component_errors={"system_audio": err},
        last_error="system_audio device lost",
    )
    s = sm.current()
    assert s.mode == "error"
    assert "system_audio" in s.component_errors
    assert s.component_errors["system_audio"].message == "device_lost"


async def test_aegis_unreachable_is_flag_not_mode():
    """Aegis unreachable is a separate boolean — it must NOT push us into mode=error."""
    sm = DaemonStateMachine()
    # Stay in armed, but flip the flag.
    await sm.transition(aegis_unreachable=True)
    s = sm.current()
    assert s.mode == "armed"
    assert s.aegis_unreachable is True

    # Flip it back off.
    await sm.transition(aegis_unreachable=False)
    s = sm.current()
    assert s.mode == "armed"
    assert s.aegis_unreachable is False


# ---------------------------------------------------------------------------
# Listener semantics
# ---------------------------------------------------------------------------


async def test_listener_fires_with_old_and_new_state():
    sm = DaemonStateMachine()
    calls: list[tuple[str, str]] = []

    def listener(old: DaemonState, new: DaemonState) -> None:
        calls.append((old.mode, new.mode))

    sm.subscribe(listener)
    await sm.transition(mode="recording", active_session_id=1)
    await sm.transition(mode="armed", active_session_id=None)

    assert calls == [("armed", "recording"), ("recording", "armed")]


async def test_sync_listener_works():
    sm = DaemonStateMachine()
    calls: list[str] = []

    def listener(old: DaemonState, new: DaemonState) -> None:
        calls.append(new.mode)

    sm.subscribe(listener)
    await sm.transition(mode="recording")
    assert calls == ["recording"]


async def test_async_listener_is_awaited():
    sm = DaemonStateMachine()
    calls: list[str] = []

    async def listener(old: DaemonState, new: DaemonState) -> None:
        # Yield to event loop to confirm we are actually awaited.
        await asyncio.sleep(0)
        calls.append(new.mode)

    sm.subscribe(listener)
    await sm.transition(mode="recording")
    assert calls == ["recording"]


async def test_listener_exception_does_not_break_others():
    sm = DaemonStateMachine()
    other_called: list[str] = []

    def bad(old: DaemonState, new: DaemonState) -> None:
        raise ValueError("boom")

    def good(old: DaemonState, new: DaemonState) -> None:
        other_called.append(new.mode)

    sm.subscribe(bad)
    sm.subscribe(good)
    new_state = await sm.transition(mode="recording", active_session_id=42)

    # The good listener still fired.
    assert other_called == ["recording"]
    # And the transition was actually committed.
    assert new_state.mode == "recording"
    assert sm.current().mode == "recording"
    assert sm.current().active_session_id == 42


async def test_async_listener_exception_does_not_break_others():
    sm = DaemonStateMachine()
    other_called: list[str] = []

    async def bad(old: DaemonState, new: DaemonState) -> None:
        raise RuntimeError("async boom")

    async def good(old: DaemonState, new: DaemonState) -> None:
        other_called.append(new.mode)

    sm.subscribe(bad)
    sm.subscribe(good)
    await sm.transition(mode="recording")
    assert other_called == ["recording"]
    assert sm.current().mode == "recording"


async def test_unsubscribe_removes_listener():
    sm = DaemonStateMachine()
    calls: list[str] = []

    def listener(old: DaemonState, new: DaemonState) -> None:
        calls.append(new.mode)

    sm.subscribe(listener)
    await sm.transition(mode="recording")
    sm.unsubscribe(listener)
    await sm.transition(mode="armed")

    assert calls == ["recording"]


async def test_unsubscribe_unknown_listener_is_noop():
    sm = DaemonStateMachine()

    def never_subscribed(old: DaemonState, new: DaemonState) -> None:
        pass

    # Must not raise.
    sm.unsubscribe(never_subscribed)


async def test_subscribe_is_idempotent():
    sm = DaemonStateMachine()
    calls: list[str] = []

    def listener(old: DaemonState, new: DaemonState) -> None:
        calls.append(new.mode)

    sm.subscribe(listener)
    sm.subscribe(listener)  # duplicate
    sm.subscribe(listener)  # triplicate

    await sm.transition(mode="recording")
    assert calls == ["recording"]  # only one call per transition


# ---------------------------------------------------------------------------
# Component error merge semantics
# ---------------------------------------------------------------------------


async def test_component_errors_merge_not_replace():
    sm = DaemonStateMachine()
    mic_err = ComponentError("mic", "permission_denied", 1.0)
    sa_err = ComponentError("system_audio", "device_lost", 2.0)

    await sm.transition(mode="error", component_errors={"mic": mic_err})
    await sm.transition(component_errors={"system_audio": sa_err})

    s = sm.current()
    assert set(s.component_errors.keys()) == {"mic", "system_audio"}
    assert s.component_errors["mic"].message == "permission_denied"
    assert s.component_errors["system_audio"].message == "device_lost"


async def test_component_errors_can_be_explicitly_cleared():
    sm = DaemonStateMachine()
    err = ComponentError("mic", "permission_denied", 1.0)
    await sm.transition(mode="error", component_errors={"mic": err})

    # Empty dict means "clear all".
    await sm.transition(component_errors={})
    assert sm.current().component_errors == {}


async def test_component_errors_overwrite_same_key_on_merge():
    sm = DaemonStateMachine()
    first = ComponentError("mic", "permission_denied", 1.0)
    second = ComponentError("mic", "device_unplugged", 2.0)

    await sm.transition(mode="error", component_errors={"mic": first})
    await sm.transition(component_errors={"mic": second})

    s = sm.current()
    assert s.component_errors["mic"].message == "device_unplugged"
    assert s.component_errors["mic"].occurred_at == 2.0


async def test_clear_component_errors_removes_named_keys():
    sm = DaemonStateMachine()
    mic_err = ComponentError("mic", "x", 1.0)
    sa_err = ComponentError("system_audio", "y", 2.0)
    tx_err = ComponentError("transcription", "z", 3.0)

    await sm.transition(
        mode="error",
        component_errors={"mic": mic_err, "system_audio": sa_err, "transcription": tx_err},
    )

    sm.clear_component_errors("mic", "transcription")
    s = sm.current()
    assert set(s.component_errors.keys()) == {"system_audio"}


async def test_clear_component_errors_with_no_args_is_noop():
    sm = DaemonStateMachine()
    err = ComponentError("mic", "x", 1.0)
    await sm.transition(mode="error", component_errors={"mic": err})

    sm.clear_component_errors()
    assert "mic" in sm.current().component_errors


# ---------------------------------------------------------------------------
# Snapshot independence
# ---------------------------------------------------------------------------


async def test_snapshot_is_independent_of_internal_state():
    """Mutating the dict on the returned snapshot does NOT affect the machine."""
    sm = DaemonStateMachine()
    err = ComponentError("mic", "x", 1.0)
    await sm.transition(mode="error", component_errors={"mic": err})

    snapshot = sm.current()
    snapshot.component_errors["forged"] = ComponentError("forged", "nope", 9.0)

    fresh = sm.current()
    assert "forged" not in fresh.component_errors
    assert set(fresh.component_errors.keys()) == {"mic"}


# ---------------------------------------------------------------------------
# transition() return value
# ---------------------------------------------------------------------------


async def test_transition_returns_new_state_snapshot():
    sm = DaemonStateMachine()
    returned = await sm.transition(mode="recording", active_session_id=7)
    assert returned.mode == "recording"
    assert returned.active_session_id == 7

    # And the returned snapshot is independent.
    returned.component_errors["x"] = ComponentError("x", "y", 0.0)
    assert "x" not in sm.current().component_errors


async def test_omitted_kwargs_leave_fields_unchanged():
    sm = DaemonStateMachine()
    await sm.transition(mode="recording", active_session_id=5, last_error="hi")

    # Now flip the mode but don't touch the other fields.
    await sm.transition(mode="error")
    s = sm.current()
    assert s.mode == "error"
    assert s.active_session_id == 5  # preserved
    assert s.last_error == "hi"  # preserved


async def test_explicit_none_clears_nullable_fields():
    sm = DaemonStateMachine()
    await sm.transition(mode="recording", active_session_id=5, last_error="hi")
    await sm.transition(active_session_id=None, last_error=None)
    s = sm.current()
    assert s.active_session_id is None
    assert s.last_error is None


# ---------------------------------------------------------------------------
# Public surface smoke
# ---------------------------------------------------------------------------


def test_public_surface_imports():
    """Confirm every name promised in the contract is exported."""
    from helios.state import (  # noqa: F401
        ComponentError,
        DaemonState,
        DaemonStateMachine,
        Mode,
        StateListener,
    )
