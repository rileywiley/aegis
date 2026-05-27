"""Clock abstraction for testability.

All time-dependent code in Helios takes a `Clock` instance via dependency
injection. `RealClock` wraps the system clock and asyncio for production use;
`VirtualClock` provides deterministic, fast-forwardable time for tests.

Per HELIOS.md §18.3, no calls to `time.time()` or `asyncio.sleep()` should
appear outside this module.
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import time
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Abstract clock interface."""

    def time(self) -> float:
        """Return current time as a UTC epoch timestamp (seconds)."""
        ...

    def now_local(self) -> datetime.datetime:
        """Return current time as a naive local datetime."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Async sleep for the given number of seconds."""
        ...

    def call_later(self, delay: float, callback: Callable[[], Any]) -> "TimerHandle":
        """Schedule `callback` to be invoked after `delay` seconds.

        Returns a handle whose `cancel()` method prevents the callback from firing.
        """
        ...


class TimerHandle(Protocol):
    """Minimal handle protocol — just needs `cancel()`."""

    def cancel(self) -> None: ...


class RealClock:
    """Production clock backed by `time.time()` and `asyncio`."""

    def time(self) -> float:
        return time.time()

    def now_local(self) -> datetime.datetime:
        return datetime.datetime.now()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def call_later(
        self, delay: float, callback: Callable[[], Any]
    ) -> asyncio.TimerHandle:
        loop = asyncio.get_event_loop()
        return loop.call_later(delay, callback)


class _VirtualTimerHandle:
    """Cancelable handle for `VirtualClock.call_later`."""

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class VirtualClock:
    """Deterministic clock for tests.

    Time only moves forward when `advance()` is called. Sleepers and
    `call_later` callbacks are tracked and woken in timestamp order.
    """

    def __init__(self, initial_ts: float) -> None:
        self._now: float = initial_ts
        # Pending sleepers as (wake_time, event) tuples.
        self._sleepers: list[tuple[float, asyncio.Event]] = []
        # Pending call_later callbacks as (fire_time, handle, callback) tuples.
        self._callbacks: list[
            tuple[float, _VirtualTimerHandle, Callable[[], Any]]
        ] = []

    def time(self) -> float:
        return self._now

    def now_local(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self._now)

    async def sleep(self, seconds: float) -> None:
        event = asyncio.Event()
        wake_at = self._now + seconds
        self._sleepers.append((wake_at, event))
        await event.wait()

    def call_later(
        self, delay: float, callback: Callable[[], Any]
    ) -> _VirtualTimerHandle:
        handle = _VirtualTimerHandle()
        fire_at = self._now + delay
        self._callbacks.append((fire_at, handle, callback))
        return handle

    async def advance(self, seconds: float) -> None:
        """Advance virtual time by `seconds`, firing due sleepers/callbacks in order."""
        target = self._now + seconds

        while True:
            # Find the next due timestamp from either sleepers or callbacks.
            due_sleeper_times = [ts for ts, _ in self._sleepers if ts <= target]
            due_callback_times = [
                ts
                for ts, handle, _ in self._callbacks
                if ts <= target and not handle.cancelled
            ]

            # Drop cancelled callbacks that are due — they should be cleaned up
            # without firing.
            cancelled_due = [
                (ts, h, cb)
                for ts, h, cb in self._callbacks
                if ts <= target and h.cancelled
            ]
            if cancelled_due:
                self._callbacks = [
                    (ts, h, cb)
                    for ts, h, cb in self._callbacks
                    if not (ts <= target and h.cancelled)
                ]

            if not due_sleeper_times and not due_callback_times:
                break

            # Earliest due timestamp wins; fire all events/callbacks at that ts.
            next_ts = min(due_sleeper_times + due_callback_times)
            self._now = next_ts

            # Collect everything due at exactly next_ts.
            due_events = [e for ts, e in self._sleepers if ts == next_ts]
            self._sleepers = [
                (ts, e) for ts, e in self._sleepers if ts != next_ts
            ]

            due_cbs = [
                (h, cb)
                for ts, h, cb in self._callbacks
                if ts == next_ts and not h.cancelled
            ]
            self._callbacks = [
                (ts, h, cb) for ts, h, cb in self._callbacks if ts != next_ts
            ]

            # Wake all sleepers due at this timestamp.
            for event in due_events:
                event.set()

            # Fire callbacks; support both sync and async callables.
            for _handle, cb in due_cbs:
                result = cb()
                if inspect.isawaitable(result):
                    await result

            # Yield to let woken coroutines run before checking for newly
            # registered sleepers/callbacks.
            await asyncio.sleep(0)

        self._now = target
