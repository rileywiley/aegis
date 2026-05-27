"""Helios calendar client — fetches upcoming meetings from Aegis.

Per HELIOS.md §16.5, Aegis exposes ``GET /api/meetings/upcoming``. This client
wraps that endpoint and converts the response into the internal
``CalendarEvent`` shape used elsewhere in Helios.

Two named exceptions surface failure modes so the scheduler can react
differently:

* :class:`AegisUnreachable` — connection refused, timeout, or 5xx. The
  scheduler keeps polling; the daemon flips ``state.aegis_unreachable=True``
  so the menu bar can show a degraded badge.
* :class:`AegisProtocolError` — non-JSON response, schema mismatch, missing
  field, or 4xx. These do not recover by retry; the scheduler logs and skips
  this poll cycle.

Retries are intentionally NOT implemented inside :class:`CalendarClient` —
the scheduler owns polling cadence and decides whether to retry next cycle.
"""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from helios.log import get_logger
from helios.sources.interface import CalendarEvent
from shared.meetings import UpcomingMeetingsResponse

_log = get_logger("scheduler.calendar")


class AegisUnreachable(Exception):
    """Raised on connection refused, timeout, or 5xx from Aegis.

    The scheduler keeps polling — the daemon flags
    ``state.aegis_unreachable=True`` so the menu bar can show a degraded
    badge.
    """


class AegisProtocolError(Exception):
    """Raised on malformed response (non-JSON, schema mismatch, missing field).

    These shouldn't recover by retry — scheduler logs and skips this poll
    cycle.
    """


class CalendarClient:
    """HTTP client for Aegis's ``/api/meetings/upcoming`` endpoint."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._timeout = timeout_seconds

    async def get_upcoming(self, horizon_minutes: int = 60) -> list[CalendarEvent]:
        """Fetch and parse upcoming meetings.

        Returns a list of internal :class:`CalendarEvent` values, sorted by
        ``start_ts``. Excluded events are filtered out client-side
        (belt-and-suspenders — the scheduler also filters).

        Raises:
            AegisUnreachable: network failure, timeout, or 5xx response.
            AegisProtocolError: non-JSON body, schema mismatch, or 4xx response.
        """
        url = f"{self._base_url}/api/meetings/upcoming"
        try:
            response = await self._http.get(
                url,
                params={"horizon_minutes": horizon_minutes},
                timeout=self._timeout,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise AegisUnreachable(f"connect to {url} failed: {exc}") from exc

        if response.status_code >= 500:
            raise AegisUnreachable(
                f"{url} returned {response.status_code}"
            )
        if response.status_code != 200:
            # 4xx is a protocol error — bad input from us or wrong endpoint.
            raise AegisProtocolError(
                f"{url} returned {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AegisProtocolError(f"{url}: non-JSON response") from exc

        try:
            data = UpcomingMeetingsResponse.model_validate(payload)
        except ValidationError as exc:
            raise AegisProtocolError(
                f"{url}: schema mismatch: {exc.errors()[:3]}"
            ) from exc

        events = sorted(
            (
                CalendarEvent(
                    id=e.calendar_event_id,
                    title=e.title,
                    start_ts=e.starts_at,
                    end_ts=e.ends_at,
                    # Aegis only sends attendee_count; Helios's scheduler
                    # doesn't use attendee identities, so pass an empty list.
                    attendees=[],
                )
                for e in data.events
                if not e.is_excluded
            ),
            key=lambda c: c.start_ts,
        )
        # NOTE: do NOT log calendar event titles — they're PII.
        _log.info(
            "fetched_upcoming",
            count=len(events),
            horizon_minutes=horizon_minutes,
            poll_interval_seconds=data.poll_interval_seconds,
        )
        return events
