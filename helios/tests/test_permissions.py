"""Tests for helios.workers.permissions — Wave 1 / 2F.

Uses the ``tmp_db`` fixture (real SQLite + migrations applied) plus the
``virtual_clock`` fixture for deterministic timing. The OS layer is
abstracted via :class:`PermissionsBackend`; tests inject a ``_FakeBackend``
so PyObjC is never imported.
"""

import asyncio

import aiosqlite
import pytest
import pytest_asyncio

from helios.db import queries
from helios.db.migrations import run_migrations
from helios.state import DaemonStateMachine
from helios.workers.permissions import (
    PermissionChecker,
    PermissionState,
    PermissionsBackend,
)


# ---------------------------------------------------------------------------
# Test backend
# ---------------------------------------------------------------------------


class _FakeBackend:
    """In-memory backend; flip attributes between checks to drive scenarios."""

    def __init__(
        self,
        mic_status: int = 3,
        screen_granted: bool = True,
        mic_raises: BaseException | None = None,
        screen_raises: BaseException | None = None,
    ) -> None:
        self.mic_status_value = mic_status
        self.screen_granted_value = screen_granted
        self.mic_raises = mic_raises
        self.screen_raises = screen_raises

    def mic_status(self) -> int:
        if self.mic_raises is not None:
            raise self.mic_raises
        return self.mic_status_value

    def screen_recording_granted(self) -> bool:
        if self.screen_raises is not None:
            raise self.screen_raises
        return self.screen_granted_value


# Structural sanity: confirm the fake exposes the Protocol's call sites.
# (The Protocol is not @runtime_checkable, so we can't isinstance-check it.)
assert callable(_FakeBackend.mic_status)
assert callable(_FakeBackend.screen_recording_granted)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_db, migrations_dir):
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


@pytest.fixture
def state_machine() -> DaemonStateMachine:
    return DaemonStateMachine()


def _make_checker(
    *,
    db,
    clock,
    sm: DaemonStateMachine,
    backend: _FakeBackend,
    interval: float = 300.0,
) -> PermissionChecker:
    return PermissionChecker(
        clock=clock,
        db=db,
        state_machine=sm,
        check_interval_seconds=interval,
        backend=backend,
    )


async def _count_rows(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM permission_checks")
    (n,) = await cursor.fetchone()
    return n


# ---------------------------------------------------------------------------
# 1-5: status mapping for each AVAuthorizationStatus value + screen
# ---------------------------------------------------------------------------


async def test_both_granted(db, virtual_clock, state_machine):
    backend = _FakeBackend(mic_status=3, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    state = await checker.check_now()

    assert isinstance(state, PermissionState)
    assert state.mic_granted is True
    assert state.screen_recording_granted is True
    assert state.checked_at == virtual_clock.time()

    # Persisted exactly one row with both granted = 1.
    cursor = await db.execute(
        "SELECT mic_granted, screen_recording_granted FROM permission_checks"
    )
    rows = await cursor.fetchall()
    assert rows == [(1, 1)]

    # State machine NOT transitioned out of the default mode.
    assert state_machine.current().mode == "armed"
    assert state_machine.current().component_errors == {}


async def test_mic_denied(db, virtual_clock, state_machine):
    backend = _FakeBackend(mic_status=2, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)
    state = await checker.check_now()
    assert state.mic_granted is False
    assert state.screen_recording_granted is True


async def test_mic_not_determined(db, virtual_clock, state_machine):
    backend = _FakeBackend(mic_status=0, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)
    state = await checker.check_now()
    assert state.mic_granted is False


async def test_mic_restricted(db, virtual_clock, state_machine):
    backend = _FakeBackend(mic_status=1, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)
    state = await checker.check_now()
    assert state.mic_granted is False


async def test_screen_denied(db, virtual_clock, state_machine):
    backend = _FakeBackend(mic_status=3, screen_granted=False)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)
    state = await checker.check_now()
    assert state.mic_granted is True
    assert state.screen_recording_granted is False


# ---------------------------------------------------------------------------
# 6-7: revocation transitions
# ---------------------------------------------------------------------------


async def test_revocation_mic_granted_to_denied(db, virtual_clock, state_machine):
    backend = _FakeBackend(mic_status=3, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    # First check: granted → baseline established. State unchanged.
    await checker.check_now()
    assert state_machine.current().mode == "armed"

    # Flip to denied.
    backend.mic_status_value = 2
    await checker.check_now()

    snap = state_machine.current()
    assert snap.mode == "error"
    assert "mic" in snap.component_errors
    assert snap.component_errors["mic"].message == "permission_revoked"
    assert snap.component_errors["mic"].component == "mic"
    # Screen was never revoked, so no entry for it.
    assert "screen_recording" not in snap.component_errors
    assert snap.last_error is not None
    assert "permission revoked" in snap.last_error
    assert "mic" in snap.last_error


async def test_revocation_screen_granted_to_denied(db, virtual_clock, state_machine):
    backend = _FakeBackend(mic_status=3, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    await checker.check_now()
    assert state_machine.current().mode == "armed"

    backend.screen_granted_value = False
    await checker.check_now()

    snap = state_machine.current()
    assert snap.mode == "error"
    assert "screen_recording" in snap.component_errors
    assert (
        snap.component_errors["screen_recording"].message == "permission_revoked"
    )
    assert "mic" not in snap.component_errors
    assert "screen_recording" in (snap.last_error or "")


# ---------------------------------------------------------------------------
# 8-9: cold-start behavior
# ---------------------------------------------------------------------------


async def test_cold_start_no_prior_does_not_fire(db, virtual_clock, state_machine):
    """First check, currently denied, no prior data → baseline only."""
    backend = _FakeBackend(mic_status=2, screen_granted=False)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    await checker.check_now()

    # State machine is unchanged (no granted→denied transition observed).
    assert state_machine.current().mode == "armed"
    assert state_machine.current().component_errors == {}
    # The check still got persisted.
    assert await _count_rows(db) == 1


async def test_cold_start_with_prior_denied_does_not_fire(
    db, virtual_clock, state_machine
):
    """Daemon restarts; previous check in DB shows denied; current also denied.

    No granted→denied transition, so no error.
    """
    # Seed DB with a prior 'denied' check.
    await queries.insert_permission_check(
        db,
        checked_at=virtual_clock.time() - 600,
        mic_granted=False,
        screen_recording_granted=False,
    )

    backend = _FakeBackend(mic_status=2, screen_granted=False)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    await checker.check_now()

    assert state_machine.current().mode == "armed"
    assert state_machine.current().component_errors == {}


async def test_cold_start_with_prior_granted_then_currently_denied_fires(
    db, virtual_clock, state_machine
):
    """Restart scenario: last check was granted, current is denied → revoke."""
    await queries.insert_permission_check(
        db,
        checked_at=virtual_clock.time() - 600,
        mic_granted=True,
        screen_recording_granted=True,
    )

    backend = _FakeBackend(mic_status=2, screen_granted=False)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    await checker.check_now()

    snap = state_machine.current()
    assert snap.mode == "error"
    assert "mic" in snap.component_errors
    assert "screen_recording" in snap.component_errors


# ---------------------------------------------------------------------------
# Restoration: revoked → granted clears the error
# ---------------------------------------------------------------------------


async def test_restoration_clears_component_error_and_recovers_mode(
    db, virtual_clock, state_machine
):
    """granted → denied → granted should drop out of mode=error without restart.

    Regression: a fresh bundle install initially shows perms as denied
    (TCC identity changed). After the user re-grants, the daemon was
    staying stuck in mode=error with stale ComponentError entries.
    """
    backend = _FakeBackend(mic_status=3, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    await checker.check_now()
    assert state_machine.current().mode == "armed"

    # Revoke both.
    backend.mic_status_value = 2
    backend.screen_granted_value = False
    await checker.check_now()
    snap = state_machine.current()
    assert snap.mode == "error"
    assert set(snap.component_errors.keys()) == {"mic", "screen_recording"}

    # Restore mic only — screen still revoked, so mode stays in error.
    backend.mic_status_value = 3
    await checker.check_now()
    snap = state_machine.current()
    assert snap.mode == "error"
    assert "mic" not in snap.component_errors
    assert "screen_recording" in snap.component_errors

    # Restore screen — all clean.
    backend.screen_granted_value = True
    await checker.check_now()
    snap = state_machine.current()
    assert snap.mode == "armed"
    assert snap.component_errors == {}
    assert snap.last_error is None


async def test_restoration_preserves_unrelated_component_errors(
    db, virtual_clock, state_machine
):
    """Granting a permission must not nuke an unrelated component error."""
    from helios.state import ComponentError

    backend = _FakeBackend(mic_status=3, screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)
    await checker.check_now()

    # Revoke mic.
    backend.mic_status_value = 2
    await checker.check_now()

    # Simulate an unrelated subsystem reporting an error (e.g. transcription).
    await state_machine.transition(
        component_errors={
            "transcription": ComponentError(
                component="transcription",
                message="model_load_failed",
                occurred_at=virtual_clock.time(),
            )
        }
    )

    # Restore mic.
    backend.mic_status_value = 3
    await checker.check_now()
    snap = state_machine.current()
    # mic error cleared, transcription error intact, mode stays in error
    # because there's still a remaining component_error.
    assert "mic" not in snap.component_errors
    assert "transcription" in snap.component_errors
    assert snap.mode == "error"


async def test_cold_start_revoke_then_restore_recovers(
    db, virtual_clock, state_machine
):
    """Bundle re-install scenario: prior=granted, current=denied at startup.

    The first check on the new daemon sees denied (new TCC identity).
    Once the user re-grants, the next check should clear the error
    without needing a daemon restart.
    """
    # Prior daemon run had perms granted.
    await queries.insert_permission_check(
        db,
        checked_at=virtual_clock.time() - 600,
        mic_granted=True,
        screen_recording_granted=True,
    )

    backend = _FakeBackend(mic_status=2, screen_granted=False)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    # First check: daemon sees revoked (new bundle, new TCC identity).
    await checker.check_now()
    assert state_machine.current().mode == "error"

    # User re-grants both in System Settings.
    backend.mic_status_value = 3
    backend.screen_granted_value = True
    await checker.check_now()

    snap = state_machine.current()
    assert snap.mode == "armed"
    assert snap.component_errors == {}


# ---------------------------------------------------------------------------
# 10: periodic loop fires repeatedly under VirtualClock
# ---------------------------------------------------------------------------


async def test_periodic_loop_fires_under_virtual_clock(
    db, virtual_clock, state_machine
):
    backend = _FakeBackend(mic_status=3, screen_granted=True)
    checker = _make_checker(
        db=db,
        clock=virtual_clock,
        sm=state_machine,
        backend=backend,
        interval=300.0,
    )

    await checker.start()

    # Yield until the first check completes and the loop is parked in
    # clock.sleep(300). Several event loop iterations are required for
    # async DB writes + state machine transitions to settle.
    for _ in range(30):
        await asyncio.sleep(0)
        if await _count_rows(db) >= 1:
            break
    assert await _count_rows(db) == 1

    # Advance 5 minutes — second iteration runs.
    await virtual_clock.advance(300.0)
    for _ in range(30):
        await asyncio.sleep(0)
        if await _count_rows(db) >= 2:
            break
    assert await _count_rows(db) == 2

    # And once more.
    await virtual_clock.advance(300.0)
    for _ in range(30):
        await asyncio.sleep(0)
        if await _count_rows(db) >= 3:
            break
    assert await _count_rows(db) == 3

    await checker.stop()


# ---------------------------------------------------------------------------
# 11: stop() is idempotent
# ---------------------------------------------------------------------------


async def test_stop_is_idempotent(db, virtual_clock, state_machine):
    backend = _FakeBackend()
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    # Stop before start: no-op.
    await checker.stop()

    await checker.start()
    # Let the first iteration record itself.
    for _ in range(10):
        await asyncio.sleep(0)
        if await _count_rows(db) >= 1:
            break

    await checker.stop()
    # Second stop is a clean no-op.
    await checker.stop()


async def test_start_is_idempotent(db, virtual_clock, state_machine):
    backend = _FakeBackend()
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    await checker.start()
    first_task = checker._task
    await checker.start()  # second call must NOT spawn a new task
    assert checker._task is first_task

    await checker.stop()


# ---------------------------------------------------------------------------
# 12: backend exception is swallowed inside check_now()
# ---------------------------------------------------------------------------


async def test_backend_mic_exception_returns_false_no_transition(
    db, virtual_clock, state_machine
):
    backend = _FakeBackend(mic_raises=RuntimeError("kaboom"), screen_granted=True)
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    state = await checker.check_now()
    assert state.mic_granted is False
    assert state.screen_recording_granted is True

    # No prior baseline → no revocation event fires.
    assert state_machine.current().mode == "armed"

    # Row was still inserted.
    cursor = await db.execute(
        "SELECT mic_granted, screen_recording_granted FROM permission_checks"
    )
    rows = await cursor.fetchall()
    assert rows == [(0, 1)]


async def test_backend_screen_exception_returns_false(db, virtual_clock, state_machine):
    backend = _FakeBackend(
        mic_status=3, screen_raises=RuntimeError("framework_unavailable")
    )
    checker = _make_checker(db=db, clock=virtual_clock, sm=state_machine, backend=backend)

    state = await checker.check_now()
    assert state.mic_granted is True
    assert state.screen_recording_granted is False
    assert state_machine.current().mode == "armed"


# ---------------------------------------------------------------------------
# 13: DB exception inside an iteration is swallowed by run_periodic
# ---------------------------------------------------------------------------


async def test_run_periodic_swallows_iteration_failures(
    db, virtual_clock, state_machine, monkeypatch
):
    """If insert_permission_check raises, run_periodic logs and continues.

    We monkeypatch ``insert_permission_check`` to raise on the first call
    and succeed afterward. The first iteration should fail silently; the
    second iteration (after clock.advance) should succeed and write a row.
    """
    backend = _FakeBackend()
    checker = _make_checker(
        db=db,
        clock=virtual_clock,
        sm=state_machine,
        backend=backend,
        interval=300.0,
    )

    call_count = {"n": 0}
    real_insert = queries.insert_permission_check

    async def flaky_insert(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise aiosqlite.OperationalError("simulated DB hiccup")
        return await real_insert(*args, **kwargs)

    # Patch the symbol the worker actually looks up.
    monkeypatch.setattr(
        "helios.workers.permissions.queries.insert_permission_check",
        flaky_insert,
    )

    await checker.start()

    # First iteration: raises, gets swallowed, loop sleeps.
    for _ in range(40):
        await asyncio.sleep(0)
        if call_count["n"] >= 1:
            break
    assert call_count["n"] >= 1
    assert await _count_rows(db) == 0  # the failing call did not commit

    # The task should still be alive — failure did not crash the loop.
    assert checker._task is not None
    assert not checker._task.done()

    # Advance to next iteration, which should succeed.
    await virtual_clock.advance(300.0)
    for _ in range(40):
        await asyncio.sleep(0)
        if await _count_rows(db) >= 1:
            break
    assert await _count_rows(db) == 1

    await checker.stop()


# ---------------------------------------------------------------------------
# Bonus: insert helper + last-check round-trip via queries module directly.
# ---------------------------------------------------------------------------


async def test_insert_and_get_last_permission_check(db):
    rid1 = await queries.insert_permission_check(
        db, checked_at=1000.0, mic_granted=True, screen_recording_granted=False
    )
    assert isinstance(rid1, int)

    last = await queries.get_last_permission_check(db)
    assert last is not None
    assert last.id == rid1
    assert last.checked_at == 1000.0
    assert last.mic_granted is True
    assert last.screen_recording_granted is False

    # Insert a newer one — get_last returns the most recent by checked_at.
    rid2 = await queries.insert_permission_check(
        db, checked_at=2000.0, mic_granted=False, screen_recording_granted=True
    )
    last2 = await queries.get_last_permission_check(db)
    assert last2.id == rid2
    assert last2.checked_at == 2000.0


async def test_get_last_permission_check_empty(db):
    assert await queries.get_last_permission_check(db) is None
