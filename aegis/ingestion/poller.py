"""Polling orchestrator — background tasks for periodic data sync."""

import asyncio
import logging
from datetime import datetime, timezone

from aegis.config import get_settings
from aegis.db.engine import async_session_factory
from aegis.db.repositories import upsert_system_health
from aegis.ingestion.calendar_sync import CalendarSync
from aegis.ingestion.graph_client import GraphClient
from aegis.ingestion.teams_poller import TeamsPoller

logger = logging.getLogger(__name__)


class Poller:
    """Manages background polling tasks for all data sources."""

    def __init__(self) -> None:
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._graph_client: GraphClient | None = None

    async def start(self) -> None:
        """Start all polling background tasks."""
        self._graph_client = GraphClient()
        self._tasks.append(asyncio.create_task(self._calendar_poll_loop()))
        self._tasks.append(asyncio.create_task(self._email_poll_loop()))
        self._tasks.append(asyncio.create_task(self._teams_poll_loop()))
        logger.info("Poller started — calendar, email, and Teams sync running")

    async def stop(self) -> None:
        """Signal shutdown and wait for all tasks to finish."""
        self._shutdown_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._graph_client:
            await self._graph_client.close()
        logger.info("Poller stopped")

    async def _calendar_poll_loop(self) -> None:
        """Periodically sync calendar events."""
        settings = get_settings()
        interval = settings.polling_calendar_seconds
        calendar_sync = CalendarSync(self._graph_client)

        while not self._shutdown_event.is_set():
            try:
                async with async_session_factory() as session:
                    count = await calendar_sync.sync(session)
                    await upsert_system_health(
                        session,
                        "calendar_sync",
                        status="healthy",
                        last_success=datetime.now(timezone.utc),
                        items_processed=count,
                    )
                logger.info("Calendar poll cycle complete — %d meetings synced", count)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Calendar poll cycle failed")
                try:
                    async with async_session_factory() as session:
                        await upsert_system_health(
                            session,
                            "calendar_sync",
                            status="degraded",
                            last_error=datetime.now(timezone.utc),
                            last_error_message="Calendar sync failed — see logs",
                        )
                except Exception:
                    logger.exception("Failed to update system_health after calendar error")

            # Wait for the interval or until shutdown is signalled
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=interval
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _email_poll_loop(self) -> None:
        """Periodically poll for new emails.

        The full EmailPoller is built by Phase 3 Agent A. This loop provides
        the scheduling scaffold so it can be wired in once available.
        """
        settings = get_settings()
        interval = settings.polling_email_seconds

        while not self._shutdown_event.is_set():
            try:
                # Import lazily — email_poller.py may not exist yet (Agent A's work)
                try:
                    from aegis.ingestion.email_poller import EmailPoller

                    poller = EmailPoller(self._graph_client)
                    async with async_session_factory() as session:
                        count = await poller.poll(session)
                        await upsert_system_health(
                            session,
                            "email_poller",
                            status="healthy",
                            last_success=datetime.now(timezone.utc),
                            items_processed=count,
                        )
                    logger.info("Email poll cycle complete — %d emails processed", count)
                except ImportError:
                    logger.debug("EmailPoller not yet available — skipping email poll cycle")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Email poll cycle failed")
                try:
                    async with async_session_factory() as session:
                        await upsert_system_health(
                            session,
                            "email_poller",
                            status="degraded",
                            last_error=datetime.now(timezone.utc),
                            last_error_message="Email poll failed — see logs",
                        )
                except Exception:
                    logger.exception("Failed to update system_health after email error")

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=interval
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _teams_poll_loop(self) -> None:
        """Periodically poll Teams chats and channels for new messages."""
        settings = get_settings()
        interval = settings.polling_teams_seconds
        teams_poller = TeamsPoller(self._graph_client)

        while not self._shutdown_event.is_set():
            try:
                async with async_session_factory() as session:
                    count = await teams_poller.poll(session)
                    await upsert_system_health(
                        session,
                        "teams_poller",
                        status="healthy",
                        last_success=datetime.now(timezone.utc),
                        items_processed=count,
                    )
                logger.info("Teams poll cycle complete — %d messages processed", count)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Teams poll cycle failed")
                try:
                    async with async_session_factory() as session:
                        await upsert_system_health(
                            session,
                            "teams_poller",
                            status="degraded",
                            last_error=datetime.now(timezone.utc),
                            last_error_message="Teams poll failed — see logs",
                        )
                except Exception:
                    logger.exception("Failed to update system_health after Teams error")

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=interval
                )
                break
            except asyncio.TimeoutError:
                pass


# Module-level convenience for starting from main.py
_poller: Poller | None = None


async def start_polling() -> None:
    """Start the global poller instance."""
    global _poller
    _poller = Poller()
    await _poller.start()


async def stop_polling() -> None:
    """Stop the global poller instance."""
    global _poller
    if _poller:
        await _poller.stop()
        _poller = None
