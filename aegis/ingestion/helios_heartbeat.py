"""Helios heartbeat loop — pings ``/v1/health`` on a fixed interval.

Per HELIOS.md §16.4 + §16.8. Writes results to the ``system_health``
table with ``service='helios'``. On the first ``healthy → down``
transition fires a macOS notification so the user knows capture has
stopped.

The loop is launched as an asyncio task from the FastAPI lifespan in
``aegis/main.py`` (see HELIOS.md §16.9). Cancellation is handled
cooperatively — the loop awaits ``asyncio.sleep(interval)`` and exits
cleanly when cancelled.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from aegis.clients.helios import HeliosClient
from aegis.db.repositories import record_helios_heartbeat

logger = logging.getLogger(__name__)


# Notifier callable returns an Awaitable so we don't import the macos
# module at the top level (keeps unit tests free of osascript).
NotifierFn = Callable[[str, str], Awaitable[None]]


async def helios_heartbeat_loop(
    client: HeliosClient,
    db_session_factory,
    config,
    notifier: NotifierFn | None = None,
) -> None:
    """Per HELIOS.md §16.8.

    Args:
        client: ``HeliosClient`` instance from ``app.state.helios_client``.
        db_session_factory: callable returning an async-context session
            (e.g. ``aegis.db.engine.async_session_factory``).
        config: settings object with ``helios_heartbeat_seconds``.
        notifier: optional async callable ``notify(title, message)``;
            when omitted falls back to ``aegis.notifications.macos.notify``
            so production runs without explicit wiring still alert the
            user.
    """
    interval = max(1, int(getattr(config, "helios_heartbeat_seconds", 60)))

    if notifier is None:
        # Lazy import — keeps test runs free of the macos notifier module.
        from aegis.notifications.macos import notify as _default_notify

        notifier = _default_notify

    last_status: str | None = None  # 'healthy' | 'down'

    while True:
        try:
            healthy = await client.health_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            # health_check() itself swallows network errors; only an
            # unexpected exception lands here. Treat as down.
            logger.warning("helios_heartbeat_unexpected_error", exc_info=True)
            healthy = False

        new_status = "healthy" if healthy else "down"

        # NOTE: ``db_session_factory()`` returns an ``AsyncSession``
        # produced by ``sqlalchemy.ext.asyncio.async_sessionmaker``, which
        # supports ``async with`` (handles commit/rollback + close). See
        # ``aegis/db/engine.py``. Don't replace with a plain assignment.
        try:
            async with db_session_factory() as session:
                await record_helios_heartbeat(
                    session,
                    healthy=healthy,
                    timestamp=datetime.now(timezone.utc),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("helios_heartbeat_db_write_failed")

        # Fire the macOS notification only on the healthy → down edge.
        # down → healthy and the very-first observation are silent.
        if last_status == "healthy" and new_status == "down":
            try:
                await notifier(
                    "Helios offline",
                    "Capture is paused; restart Helios to resume.",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "helios_down_notification_failed", exc_info=True
                )

        last_status = new_status

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
