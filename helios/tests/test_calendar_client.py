"""Tests for the Helios calendar client (Track 2B).

These tests use ``httpx.MockTransport`` to simulate Aegis's
``/api/meetings/upcoming`` endpoint without a live server. The schema being
asserted matches HELIOS.md §16.5 — the spec contract — which Wave 1 / Agent 2A
is rewriting ``shared/src/shared/meetings.py`` to match in parallel. If these
tests run before that rewrite lands, the schema-mismatch tests will be
extra-noisy (the validation error will fire on every fixture); after the
rewrite they all pass cleanly.
"""

from __future__ import annotations

import httpx
import pytest

from helios.scheduler.calendar import (
    AegisProtocolError,
    AegisUnreachable,
    CalendarClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(handler) -> tuple[CalendarClient, httpx.AsyncClient]:
    """Wire a CalendarClient to a MockTransport-backed AsyncClient."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = CalendarClient(
        base_url="http://aegis.test",
        http=http,
        timeout_seconds=2.0,
    )
    return client, http


def _event(
    *,
    calendar_event_id: str,
    title: str,
    starts_at: float,
    ends_at: float,
    is_excluded: bool = False,
    is_online_meeting: bool = True,
    series_master_id: str | None = None,
    attendee_count: int = 1,
    exclusion_reason: str | None = None,
) -> dict:
    """Build a single event dict matching the spec contract."""
    return {
        "calendar_event_id": calendar_event_id,
        "title": title,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "is_online_meeting": is_online_meeting,
        "is_excluded": is_excluded,
        "exclusion_reason": exclusion_reason,
        "series_master_id": series_master_id,
        "attendee_count": attendee_count,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_returns_calendar_events():
    """Single event in response → list with one CalendarEvent in correct shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "events": [
                    _event(
                        calendar_event_id="evt-1",
                        title="Standup",
                        starts_at=1700000000.0,
                        ends_at=1700001800.0,
                        series_master_id="series-A",
                        attendee_count=3,
                    ),
                ],
                "poll_interval_seconds": 60,
            },
        )

    client, http = _make_client(handler)
    try:
        events = await client.get_upcoming(horizon_minutes=60)
        assert len(events) == 1
        assert events[0].id == "evt-1"
        assert events[0].title == "Standup"
        assert events[0].start_ts == 1700000000.0
        assert events[0].end_ts == 1700001800.0
        # Helios drops the attendee identities — only count is exposed by Aegis.
        assert events[0].attendees == []
    finally:
        await http.aclose()


async def test_multiple_events_returned_sorted_by_start_ts():
    """Events that arrive out of order in the response come back sorted."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "events": [
                    _event(
                        calendar_event_id="late",
                        title="Late",
                        starts_at=2000.0,
                        ends_at=3000.0,
                    ),
                    _event(
                        calendar_event_id="early",
                        title="Early",
                        starts_at=500.0,
                        ends_at=1000.0,
                    ),
                    _event(
                        calendar_event_id="middle",
                        title="Middle",
                        starts_at=1200.0,
                        ends_at=1800.0,
                    ),
                ],
                "poll_interval_seconds": 60,
            },
        )

    client, http = _make_client(handler)
    try:
        events = await client.get_upcoming()
        assert [e.id for e in events] == ["early", "middle", "late"]
    finally:
        await http.aclose()


async def test_excluded_events_filtered_out():
    """Events with ``is_excluded=True`` are dropped client-side."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "events": [
                    _event(
                        calendar_event_id="kept",
                        title="Real Meeting",
                        starts_at=1000.0,
                        ends_at=2000.0,
                        is_excluded=False,
                    ),
                    _event(
                        calendar_event_id="dropped",
                        title="HR confidential",
                        starts_at=500.0,
                        ends_at=900.0,
                        is_excluded=True,
                        exclusion_reason="keyword:HR",
                    ),
                ],
                "poll_interval_seconds": 60,
            },
        )

    client, http = _make_client(handler)
    try:
        events = await client.get_upcoming()
        assert [e.id for e in events] == ["kept"]
    finally:
        await http.aclose()


async def test_empty_events_list_returns_empty():
    """An empty ``events: []`` response yields an empty list, no error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"events": [], "poll_interval_seconds": 60},
        )

    client, http = _make_client(handler)
    try:
        events = await client.get_upcoming()
        assert events == []
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Network failures → AegisUnreachable
# ---------------------------------------------------------------------------


async def test_connection_refused_raises_unreachable():
    """A ConnectError from the transport surfaces as AegisUnreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, http = _make_client(handler)
    try:
        with pytest.raises(AegisUnreachable, match="connect to"):
            await client.get_upcoming()
    finally:
        await http.aclose()


async def test_timeout_raises_unreachable():
    """A TimeoutException from the transport surfaces as AegisUnreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client, http = _make_client(handler)
    try:
        with pytest.raises(AegisUnreachable, match="connect to"):
            await client.get_upcoming()
    finally:
        await http.aclose()


async def test_500_response_raises_unreachable():
    """A 500 from Aegis is treated as transient (Unreachable, not Protocol)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    client, http = _make_client(handler)
    try:
        with pytest.raises(AegisUnreachable, match="500"):
            await client.get_upcoming()
    finally:
        await http.aclose()


async def test_503_response_raises_unreachable():
    """503 (service unavailable) is also treated as transient."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client, http = _make_client(handler)
    try:
        with pytest.raises(AegisUnreachable, match="503"):
            await client.get_upcoming()
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Protocol failures → AegisProtocolError
# ---------------------------------------------------------------------------


async def test_404_response_raises_protocol_error():
    """A 404 is a permanent protocol error — we hit the wrong endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client, http = _make_client(handler)
    try:
        with pytest.raises(AegisProtocolError, match="404"):
            await client.get_upcoming()
    finally:
        await http.aclose()


async def test_non_json_body_raises_protocol_error():
    """200 with a non-JSON body raises AegisProtocolError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
        )

    client, http = _make_client(handler)
    try:
        with pytest.raises(AegisProtocolError, match="non-JSON"):
            await client.get_upcoming()
    finally:
        await http.aclose()


async def test_schema_mismatch_raises_protocol_error():
    """200 with valid JSON but missing the ``events`` field raises a protocol error."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Missing required `events` — Pydantic validation must fire.
        return httpx.Response(200, json={"poll_interval_seconds": 60})

    client, http = _make_client(handler)
    try:
        with pytest.raises(AegisProtocolError, match="schema mismatch"):
            await client.get_upcoming()
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


async def test_horizon_minutes_passed_in_query_string():
    """horizon_minutes is forwarded as a query-string param."""
    seen: dict[str, str | None] = {"horizon_minutes": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["horizon_minutes"] = request.url.params.get("horizon_minutes")
        return httpx.Response(
            200,
            json={"events": [], "poll_interval_seconds": 60},
        )

    client, http = _make_client(handler)
    try:
        await client.get_upcoming(horizon_minutes=120)
    finally:
        await http.aclose()

    assert seen["horizon_minutes"] == "120"
