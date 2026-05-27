"""Tests for the Helios heartbeat loop.

Covers:
  * healthy → 'healthy' row in system_health
  * unreachable → 'down' row in system_health
  * healthy → down transition fires the notifier exactly once
  * down → healthy transition does NOT fire the notifier
  * cancellation is clean
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aegis.ingestion.helios_heartbeat import helios_heartbeat_loop


class _FakeSessionFactory:
    """Async-context callable that yields the same AsyncMock every time."""

    def __init__(self):
        self.session = AsyncMock()

    def __call__(self):
        outer = self.session

        @asynccontextmanager
        async def _ctx():
            yield outer

        return _ctx()


@pytest.fixture
def config():
    # Fast loop so tests don't sleep noticeably.
    return SimpleNamespace(helios_heartbeat_seconds=1)


async def _run_loop_for(loop_coro_factory, ticks: int):
    """Start the heartbeat loop, allow N ticks, then cancel cleanly.

    Patches the ``asyncio.sleep`` inside the heartbeat module so each
    tick yields immediately and increments a counter; once ``ticks``
    ticks have elapsed the loop is cancelled.
    """
    counter = {"n": 0}
    # Capture the real sleep BEFORE we monkeypatch, so the patched
    # version can call into it without recursing.
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        counter["n"] += 1
        if counter["n"] >= ticks:
            raise asyncio.CancelledError()
        await real_sleep(0)

    import aegis.ingestion.helios_heartbeat as hh
    hh.asyncio.sleep = _fast_sleep  # type: ignore[attr-defined]
    try:
        task = asyncio.create_task(loop_coro_factory())
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        hh.asyncio.sleep = real_sleep  # type: ignore[attr-defined]


# ── Status writes ────────────────────────────────────────────


class TestStatusWrites:
    async def test_healthy_writes_healthy_status(self, config):
        client = AsyncMock()
        client.health_check = AsyncMock(return_value=True)
        notifier = AsyncMock()
        factory = _FakeSessionFactory()

        called_args = []

        async def fake_record(session, *, healthy, timestamp=None):
            called_args.append({"healthy": healthy})

        import aegis.ingestion.helios_heartbeat as hh

        original = hh.record_helios_heartbeat
        hh.record_helios_heartbeat = fake_record  # type: ignore[assignment]
        try:
            await _run_loop_for(
                lambda: helios_heartbeat_loop(client, factory, config, notifier=notifier),
                ticks=1,
            )
        finally:
            hh.record_helios_heartbeat = original  # type: ignore[assignment]

        assert any(c["healthy"] is True for c in called_args)

    async def test_unhealthy_writes_down_status(self, config):
        client = AsyncMock()
        client.health_check = AsyncMock(return_value=False)
        notifier = AsyncMock()
        factory = _FakeSessionFactory()

        called_args = []

        async def fake_record(session, *, healthy, timestamp=None):
            called_args.append({"healthy": healthy})

        import aegis.ingestion.helios_heartbeat as hh

        original = hh.record_helios_heartbeat
        hh.record_helios_heartbeat = fake_record  # type: ignore[assignment]
        try:
            await _run_loop_for(
                lambda: helios_heartbeat_loop(client, factory, config, notifier=notifier),
                ticks=1,
            )
        finally:
            hh.record_helios_heartbeat = original  # type: ignore[assignment]

        assert any(c["healthy"] is False for c in called_args)


# ── Transition notifier ──────────────────────────────────────


class TestTransitionNotifier:
    async def test_healthy_to_down_fires_notifier(self, config):
        client = AsyncMock()
        # Tick 1 healthy, tick 2 down.
        client.health_check = AsyncMock(side_effect=[True, False])
        notifier = AsyncMock()
        factory = _FakeSessionFactory()

        async def fake_record(session, *, healthy, timestamp=None):
            return None

        import aegis.ingestion.helios_heartbeat as hh

        original = hh.record_helios_heartbeat
        hh.record_helios_heartbeat = fake_record  # type: ignore[assignment]
        try:
            await _run_loop_for(
                lambda: helios_heartbeat_loop(client, factory, config, notifier=notifier),
                ticks=2,
            )
        finally:
            hh.record_helios_heartbeat = original  # type: ignore[assignment]

        # Notifier fires on the healthy → down edge, exactly once.
        assert notifier.await_count == 1
        title, message = notifier.await_args.args
        assert "Helios" in title
        assert "Capture" in message or "restart" in message.lower()

    async def test_down_to_healthy_does_not_fire_notifier(self, config):
        client = AsyncMock()
        # Tick 1 down, tick 2 healthy. No notification expected.
        client.health_check = AsyncMock(side_effect=[False, True])
        notifier = AsyncMock()
        factory = _FakeSessionFactory()

        async def fake_record(session, *, healthy, timestamp=None):
            return None

        import aegis.ingestion.helios_heartbeat as hh

        original = hh.record_helios_heartbeat
        hh.record_helios_heartbeat = fake_record  # type: ignore[assignment]
        try:
            await _run_loop_for(
                lambda: helios_heartbeat_loop(client, factory, config, notifier=notifier),
                ticks=2,
            )
        finally:
            hh.record_helios_heartbeat = original  # type: ignore[assignment]

        notifier.assert_not_awaited()

    async def test_first_observation_down_does_not_fire(self, config):
        """The very first probe being 'down' must NOT trigger a notification.

        Only the explicit healthy → down edge fires; otherwise the user
        gets spammed at every cold start when Helios isn't running yet.
        """
        client = AsyncMock()
        client.health_check = AsyncMock(side_effect=[False, False])
        notifier = AsyncMock()
        factory = _FakeSessionFactory()

        async def fake_record(session, *, healthy, timestamp=None):
            return None

        import aegis.ingestion.helios_heartbeat as hh

        original = hh.record_helios_heartbeat
        hh.record_helios_heartbeat = fake_record  # type: ignore[assignment]
        try:
            await _run_loop_for(
                lambda: helios_heartbeat_loop(client, factory, config, notifier=notifier),
                ticks=2,
            )
        finally:
            hh.record_helios_heartbeat = original  # type: ignore[assignment]

        notifier.assert_not_awaited()


# ── Cancellation ─────────────────────────────────────────────


class TestCancellation:
    async def test_cancel_during_sleep_is_clean(self, config):
        client = AsyncMock()
        client.health_check = AsyncMock(return_value=True)
        notifier = AsyncMock()
        factory = _FakeSessionFactory()

        async def fake_record(session, *, healthy, timestamp=None):
            return None

        import aegis.ingestion.helios_heartbeat as hh

        original = hh.record_helios_heartbeat
        hh.record_helios_heartbeat = fake_record  # type: ignore[assignment]
        try:
            task = asyncio.create_task(
                helios_heartbeat_loop(client, factory, config, notifier=notifier)
            )
            # Yield once so the loop runs an iteration and reaches sleep().
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            hh.record_helios_heartbeat = original  # type: ignore[assignment]
