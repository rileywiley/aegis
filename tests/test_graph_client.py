"""Tests for the Microsoft Graph API client."""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aegis.ingestion.graph_client import GraphClient, GRAPH_BASE_URL

FIXTURES = Path(__file__).parent / "fixtures"


# ── Helpers ──────────────────────────────────────────────


def _make_settings(**overrides):
    """Create a mock settings object with sensible defaults."""
    defaults = {
        "azure_client_id": "test-client-id",
        "azure_tenant_id": "test-tenant-id",
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ── Token tests ──────────────────────────────────────────


@patch("aegis.ingestion.graph_client.get_settings")
@patch("aegis.ingestion.graph_client.msal.PublicClientApplication")
@patch("aegis.ingestion.graph_client._load_msal_cache")
def test_acquire_token_silent_success(mock_cache, mock_msal_cls, mock_settings):
    """Silent token refresh returns cached access_token."""
    mock_settings.return_value = _make_settings()
    mock_cache.return_value = MagicMock()

    mock_app = MagicMock()
    mock_msal_cls.return_value = mock_app
    mock_app.get_accounts.return_value = [{"username": "user@test.com"}]
    mock_app.acquire_token_silent.return_value = {"access_token": "tok-123"}

    with patch("aegis.ingestion.graph_client._save_msal_cache"):
        client = GraphClient()
        token = client.acquire_token_silent()

    assert token == "tok-123"


@patch("aegis.ingestion.graph_client.get_settings")
@patch("aegis.ingestion.graph_client.msal.PublicClientApplication")
@patch("aegis.ingestion.graph_client._load_msal_cache")
def test_acquire_token_silent_no_accounts(mock_cache, mock_msal_cls, mock_settings):
    """Silent refresh returns None when no cached accounts exist."""
    mock_settings.return_value = _make_settings()
    mock_cache.return_value = MagicMock()

    mock_app = MagicMock()
    mock_msal_cls.return_value = mock_app
    mock_app.get_accounts.return_value = []

    client = GraphClient()
    token = client.acquire_token_silent()

    assert token is None


@patch("aegis.ingestion.graph_client.get_settings")
@patch("aegis.ingestion.graph_client.msal.PublicClientApplication")
@patch("aegis.ingestion.graph_client._load_msal_cache")
def test_device_code_flow(mock_cache, mock_msal_cls, mock_settings):
    """Device code flow acquires token when silent fails."""
    mock_settings.return_value = _make_settings()
    mock_cache.return_value = MagicMock()

    mock_app = MagicMock()
    mock_msal_cls.return_value = mock_app
    mock_app.initiate_device_flow.return_value = {
        "user_code": "ABCD1234",
        "message": "Go to https://...",
    }
    mock_app.acquire_token_by_device_flow.return_value = {"access_token": "tok-device-456"}

    with patch("aegis.ingestion.graph_client._save_msal_cache"):
        client = GraphClient()
        token = client.acquire_token_device_code()

    assert token == "tok-device-456"


# ── Pagination test ──────────────────────────────────────


@patch("aegis.ingestion.graph_client.get_settings")
@patch("aegis.ingestion.graph_client.msal.PublicClientApplication")
@patch("aegis.ingestion.graph_client._load_msal_cache")
async def test_pagination_follows_next_link(mock_cache, mock_msal_cls, mock_settings):
    """_get_paginated follows @odata.nextLink and returns all items."""
    mock_settings.return_value = _make_settings()
    mock_cache.return_value = MagicMock()

    mock_app = MagicMock()
    mock_msal_cls.return_value = mock_app
    mock_app.get_accounts.return_value = [{"username": "u@t.com"}]
    mock_app.acquire_token_silent.return_value = {"access_token": "tok"}

    page1 = {
        "value": [{"id": "a"}, {"id": "b"}],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-page",
    }
    page2 = {
        "value": [{"id": "c"}],
    }

    call_count = 0

    async def mock_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            body = page1
        else:
            body = page2
        resp = httpx.Response(200, json=body, request=httpx.Request(method, url))
        return resp

    with patch("aegis.ingestion.graph_client._save_msal_cache"):
        client = GraphClient()
        client._http = MagicMock()
        client._http.request = mock_request

        results = await client._get_paginated("https://graph.microsoft.com/v1.0/test")

    assert len(results) == 3
    assert [r["id"] for r in results] == ["a", "b", "c"]
    assert call_count == 2


# ── 429 rate limit test ──────────────────────────────────


@patch("aegis.ingestion.graph_client.get_settings")
@patch("aegis.ingestion.graph_client.msal.PublicClientApplication")
@patch("aegis.ingestion.graph_client._load_msal_cache")
async def test_429_retry(mock_cache, mock_msal_cls, mock_settings):
    """_request retries on 429 with Retry-After header."""
    mock_settings.return_value = _make_settings()
    mock_cache.return_value = MagicMock()

    mock_app = MagicMock()
    mock_msal_cls.return_value = mock_app
    mock_app.get_accounts.return_value = [{"username": "u@t.com"}]
    mock_app.acquire_token_silent.return_value = {"access_token": "tok"}

    call_count = 0

    async def mock_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                request=httpx.Request(method, url),
            )
        return httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request(method, url),
        )

    with patch("aegis.ingestion.graph_client._save_msal_cache"):
        client = GraphClient()
        client._http = MagicMock()
        client._http.request = mock_request

        result = await client._request("GET", "https://graph.microsoft.com/v1.0/me")

    assert result == {"ok": True}
    assert call_count == 2


# ── Calendar events fetch ────────────────────────────────


@patch("aegis.ingestion.graph_client.get_settings")
@patch("aegis.ingestion.graph_client.msal.PublicClientApplication")
@patch("aegis.ingestion.graph_client._load_msal_cache")
async def test_get_calendar_events(mock_cache, mock_msal_cls, mock_settings):
    """get_calendar_events calls the correct URL and returns event list."""
    mock_settings.return_value = _make_settings()
    mock_cache.return_value = MagicMock()

    mock_app = MagicMock()
    mock_msal_cls.return_value = mock_app
    mock_app.get_accounts.return_value = [{"username": "u@t.com"}]
    mock_app.acquire_token_silent.return_value = {"access_token": "tok"}

    fixture = _load_fixture("graph_calendar_events.json")

    captured_urls = []

    async def mock_request(method, url, **kwargs):
        captured_urls.append(url)
        return httpx.Response(200, json=fixture, request=httpx.Request(method, url))

    with patch("aegis.ingestion.graph_client._save_msal_cache"):
        client = GraphClient()
        client._http = MagicMock()
        client._http.request = mock_request

        events = await client.get_calendar_events(
            "2026-04-15T00:00:00Z", "2026-04-17T00:00:00Z"
        )

    assert len(events) == 9  # all events from fixture
    assert f"{GRAPH_BASE_URL}/me/calendarView" in captured_urls[0]


# ── Calendar filtering tests ─────────────────────────────


class TestCalendarFiltering:
    """Test that CalendarSync._should_skip applies all spec filtering rules."""

    def _make_sync(self):
        from aegis.ingestion.calendar_sync import CalendarSync

        mock_graph = MagicMock()
        return CalendarSync(mock_graph)

    def _base_event(self, **overrides) -> dict:
        event = {
            "id": "test-id",
            "subject": "Test Meeting",
            "start": {"dateTime": "2026-04-15T14:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-04-15T14:30:00.0000000", "timeZone": "UTC"},
            "isAllDay": False,
            "isCancelled": False,
            "isOnlineMeeting": True,
            "showAs": "busy",
            "responseStatus": {"response": "accepted"},
            "attendees": [
                {"emailAddress": {"address": "a@co.com", "name": "A"}},
                {"emailAddress": {"address": "b@co.com", "name": "B"}},
            ],
            "organizer": {"emailAddress": {"address": "a@co.com", "name": "A"}},
        }
        event.update(overrides)
        return event

    def test_normal_meeting_kept(self):
        sync = self._make_sync()
        event = self._base_event()
        assert sync._should_skip(event, []) is False

    def test_all_day_skipped(self):
        sync = self._make_sync()
        event = self._base_event(isAllDay=True)
        assert sync._should_skip(event, []) is True

    def test_cancelled_skipped(self):
        sync = self._make_sync()
        event = self._base_event(isCancelled=True)
        assert sync._should_skip(event, []) is True

    def test_declined_skipped(self):
        sync = self._make_sync()
        event = self._base_event(responseStatus={"response": "declined"})
        assert sync._should_skip(event, []) is True

    def test_oof_skipped(self):
        sync = self._make_sync()
        event = self._base_event(showAs="oof")
        assert sync._should_skip(event, []) is True

    def test_free_skipped(self):
        sync = self._make_sync()
        event = self._base_event(showAs="free")
        assert sync._should_skip(event, []) is True

    def test_solo_block_skipped(self):
        """Solo block: <=1 attendee AND not online meeting."""
        sync = self._make_sync()
        event = self._base_event(attendees=[], isOnlineMeeting=False)
        assert sync._should_skip(event, []) is True

    def test_solo_online_meeting_kept(self):
        """Solo attendee but online meeting should be kept."""
        sync = self._make_sync()
        event = self._base_event(
            attendees=[{"emailAddress": {"address": "a@co.com", "name": "A"}}],
            isOnlineMeeting=True,
        )
        assert sync._should_skip(event, []) is False

    def test_keyword_exclusion(self):
        sync = self._make_sync()
        event = self._base_event(subject="HR Performance Review - Q2")
        assert sync._should_skip(event, ["performance review"]) is True

    def test_keyword_exclusion_case_insensitive(self):
        sync = self._make_sync()
        event = self._base_event(subject="CONFIDENTIAL Board Meeting")
        assert sync._should_skip(event, ["confidential"]) is True

    def test_keyword_no_match_kept(self):
        sync = self._make_sync()
        event = self._base_event(subject="Product Roadmap Review")
        assert sync._should_skip(event, ["confidential", "hr"]) is False


class TestEventToMeetingData:
    """Test conversion of Graph API event to meeting table data."""

    def _make_sync(self):
        from aegis.ingestion.calendar_sync import CalendarSync

        mock_graph = MagicMock()
        return CalendarSync(mock_graph)

    def test_basic_conversion(self):
        sync = self._make_sync()
        event = {
            "id": "evt-001",
            "subject": "Sprint Planning",
            "start": {"dateTime": "2026-04-15T16:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-04-15T17:00:00.0000000", "timeZone": "UTC"},
            "isOnlineMeeting": True,
            "onlineMeetingUrl": "https://teams.example.com/join",
            "onlineMeeting": {"joinUrl": "https://teams.example.com/join"},
            "organizer": {"emailAddress": {"address": "dave@co.com", "name": "Dave"}},
            "seriesMasterId": "series-master-001",
        }
        data = sync._event_to_meeting_data(event)

        assert data["title"] == "Sprint Planning"
        assert data["duration"] == 60
        assert data["meeting_type"] == "virtual"
        assert data["calendar_event_id"] == "evt-001"
        assert data["online_meeting_url"] == "https://teams.example.com/join"
        assert data["organizer_email"] == "dave@co.com"
        assert data["recurring_series_id"] == "series-master-001"
        assert data["status"] == "scheduled"

    def test_in_person_meeting(self):
        sync = self._make_sync()
        event = {
            "id": "evt-002",
            "subject": "Lunch Meeting",
            "start": {"dateTime": "2026-04-15T12:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-04-15T13:00:00.0000000", "timeZone": "UTC"},
            "isOnlineMeeting": False,
            "onlineMeetingUrl": None,
            "onlineMeeting": None,
            "organizer": {"emailAddress": {"address": "a@co.com", "name": "A"}},
            "seriesMasterId": None,
        }
        data = sync._event_to_meeting_data(event)
        assert data["meeting_type"] == "in_person"
        assert data["online_meeting_url"] is None

    def test_no_subject(self):
        sync = self._make_sync()
        event = {
            "id": "evt-003",
            "subject": None,
            "start": {"dateTime": "2026-04-15T10:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-04-15T10:30:00.0000000", "timeZone": "UTC"},
            "isOnlineMeeting": False,
            "onlineMeetingUrl": None,
            "onlineMeeting": None,
            "organizer": {"emailAddress": {"address": "a@co.com", "name": "A"}},
            "seriesMasterId": None,
        }
        data = sync._event_to_meeting_data(event)
        assert data["title"] == "(No Subject)"


class TestFixtureFiltering:
    """Integration test: apply filtering to the full fixture file."""

    def _make_sync(self):
        from aegis.ingestion.calendar_sync import CalendarSync

        mock_graph = MagicMock()
        return CalendarSync(mock_graph)

    def test_fixture_filtering(self):
        """Only the normal meeting and recurring instance should pass all filters."""
        fixture = _load_fixture("graph_calendar_events.json")
        events = fixture["value"]
        sync = self._make_sync()

        # Use the default exclusion keywords from config
        exclusion_keywords = [
            kw.strip().lower()
            for kw in (
                "confidential,HR,performance review,legal,board session,"
                "personnel,disciplinary,termination"
            ).split(",")
            if kw.strip()
        ]

        kept = [e for e in events if not sync._should_skip(e, exclusion_keywords)]
        kept_ids = [e["id"] for e in kept]

        # evt-normal-001: normal meeting with 3 attendees, online -> KEEP
        assert "evt-normal-001" in kept_ids
        # evt-allday-002: all-day -> SKIP
        assert "evt-allday-002" not in kept_ids
        # evt-cancelled-003: cancelled -> SKIP
        assert "evt-cancelled-003" not in kept_ids
        # evt-solo-004: 0 attendees, not online -> SKIP
        assert "evt-solo-004" not in kept_ids
        # evt-recurring-005: recurring, 2 attendees, online -> KEEP
        assert "evt-recurring-005" in kept_ids
        # evt-declined-006: declined -> SKIP
        assert "evt-declined-006" not in kept_ids
        # evt-oof-007: showAs=oof -> SKIP
        assert "evt-oof-007" not in kept_ids
        # evt-excluded-008: subject contains "performance review" -> SKIP
        assert "evt-excluded-008" not in kept_ids
        # evt-free-009: showAs=free -> SKIP
        assert "evt-free-009" not in kept_ids

        assert len(kept) == 2
