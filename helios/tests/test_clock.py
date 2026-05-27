"""Tests for the Clock abstraction (RealClock + VirtualClock)."""

import asyncio
import time

import pytest

from helios.clock import RealClock, VirtualClock


# ---------------------------------------------------------------------------
# RealClock
# ---------------------------------------------------------------------------


async def test_realclock_time_increases():
    """RealClock.time() returns monotonically increasing wall-clock values."""
    clock = RealClock()
    t0 = clock.time()
    await asyncio.sleep(0.001)
    t1 = clock.time()
    assert t1 > t0


async def test_realclock_call_later_returns_cancelable_handle():
    """RealClock.call_later returns an asyncio.TimerHandle whose cancel() works."""
    clock = RealClock()

    fired = False

    def cb() -> None:
        nonlocal fired
        fired = True

    handle = clock.call_later(60.0, cb)
    assert isinstance(handle, asyncio.TimerHandle)
    handle.cancel()
    # Confirm the callback never runs after cancel by yielding briefly.
    await asyncio.sleep(0)
    assert fired is False


# ---------------------------------------------------------------------------
# VirtualClock — basics
# ---------------------------------------------------------------------------


async def test_virtualclock_initial_time(virtual_clock):
    """VirtualClock.time() returns the seeded initial_ts before any advance."""
    assert virtual_clock.time() == 1712345000.0


async def test_virtualclock_advance_wakes_due_sleeper(virtual_clock):
    """A sleeper whose wake time is <= advance target wakes; one beyond stays blocked."""
    woke_short = False
    woke_long = False

    async def short_sleeper() -> None:
        nonlocal woke_short
        await virtual_clock.sleep(5.0)
        woke_short = True

    async def long_sleeper() -> None:
        nonlocal woke_long
        await virtual_clock.sleep(100.0)
        woke_long = True

    short_task = asyncio.create_task(short_sleeper())
    long_task = asyncio.create_task(long_sleeper())

    # Let both register their sleeps.
    await asyncio.sleep(0)

    await virtual_clock.advance(10.0)

    # Give the short sleeper a chance to finish.
    await asyncio.sleep(0)

    assert woke_short is True
    assert woke_long is False
    assert virtual_clock.time() == 1712345010.0

    # Cleanup the still-blocked task.
    long_task.cancel()
    try:
        await long_task
    except asyncio.CancelledError:
        pass

    await short_task


async def test_virtualclock_sleep_blocks_until_advance(virtual_clock):
    """sleep() does not return until advance() crosses its wake time."""
    order: list[str] = []

    async def sleeper() -> None:
        await virtual_clock.sleep(3.0)
        order.append("woke")

    async def advancer() -> None:
        # Yield first so the sleeper registers.
        await asyncio.sleep(0)
        order.append("advance")
        await virtual_clock.advance(5.0)

    await asyncio.gather(sleeper(), advancer())

    # The advancer must record "advance" before the sleeper records "woke".
    assert order == ["advance", "woke"]


async def test_virtualclock_multiple_sleepers_all_wake(virtual_clock):
    """Multiple coroutines sleeping on the same clock all wake on a single advance."""
    woke: list[int] = []

    async def sleeper(idx: int, delay: float) -> None:
        await virtual_clock.sleep(delay)
        woke.append(idx)

    tasks = [
        asyncio.create_task(sleeper(0, 1.0)),
        asyncio.create_task(sleeper(1, 2.0)),
        asyncio.create_task(sleeper(2, 5.0)),
        asyncio.create_task(sleeper(3, 5.0)),  # same ts as #2
    ]
    await asyncio.sleep(0)

    await virtual_clock.advance(10.0)
    await asyncio.gather(*tasks)

    assert sorted(woke) == [0, 1, 2, 3]
    assert virtual_clock.time() == 1712345010.0


# ---------------------------------------------------------------------------
# VirtualClock — call_later
# ---------------------------------------------------------------------------


async def test_virtualclock_call_later_fires_in_order(virtual_clock):
    """Callbacks fire in timestamp order during advance, never before."""
    fired: list[str] = []

    virtual_clock.call_later(5.0, lambda: fired.append("five"))
    virtual_clock.call_later(2.0, lambda: fired.append("two"))
    virtual_clock.call_later(8.0, lambda: fired.append("eight"))

    # Nothing has fired yet.
    assert fired == []

    await virtual_clock.advance(3.0)
    assert fired == ["two"]

    await virtual_clock.advance(10.0)
    assert fired == ["two", "five", "eight"]


async def test_virtualclock_cancelled_callback_does_not_fire(virtual_clock):
    """A call_later handle cancelled before its wake time does not invoke the callback."""
    fired: list[str] = []

    handle_a = virtual_clock.call_later(2.0, lambda: fired.append("a"))
    virtual_clock.call_later(4.0, lambda: fired.append("b"))

    handle_a.cancel()

    await virtual_clock.advance(10.0)
    assert fired == ["b"]


# ---------------------------------------------------------------------------
# VirtualClock — performance
# ---------------------------------------------------------------------------


async def test_virtualclock_advance_is_fast(virtual_clock):
    """30 minutes of simulated time advances in under 100ms wall clock."""
    wall_start = time.perf_counter()
    await virtual_clock.advance(1800.0)  # 30 minutes
    wall_elapsed = time.perf_counter() - wall_start
    assert wall_elapsed < 0.1, f"advance took {wall_elapsed * 1000:.1f}ms"
    assert virtual_clock.time() == 1712345000.0 + 1800.0
