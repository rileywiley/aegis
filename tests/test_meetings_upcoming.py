"""Tests for GET /api/meetings/upcoming — the Helios scheduler contract.

Mirrors HELIOS.md §16.5 + the exclusion rules in `aegis/web/routes/api.py`.
DB session is mocked; we only assert the route + helpers wire up correctly.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from aegis.db.engine import get_session
from aegis.main import app


# ── Helpers ────────────────────────────────────────────────


def _make_meeting(
    *,
    calendar_event_id: str = "event-1",
    title: str = "Weekly Sync",
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    is_excluded: bool = False,
    helios_exclude: bool | None = None,
    online_meeting_url: str | None = "https://teams.example/123",
    recurring_series_id: str | None = None,
    attendees: list | None = None,
) -> SimpleNamespace:
    """Build a meeting-shaped object the route can read.

    SimpleNamespace mimics the ORM duck-type without needing a full ORM
    instance + a real session.
    """
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        calendar_event_id=calendar_event_id,
        title=title,
        start_time=start_time or (now + timedelta(minutes=10)),
        end_time=end_time or (now + timedelta(minutes=40)),
        is_excluded=is_excluded,
        helios_exclude=helios_exclude,
        online_meeting_url=online_meeting_url,
        recurring_series_id=recurring_series_id,
        attendees=attendees if attendees is not None else [object(), object()],
    )


@pytest.fixture
def client():
    """TestClient with the DB session dependency overridden to a no-op."""
    async def _fake_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = _fake_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


# ── Tests ──────────────────────────────────────────────────


class TestMeetingsUpcoming:
    def test_empty_calendar(self, client):
        """No meetings in window → events: [], poll_interval_seconds: 60."""
        with patch(
            "aegis.web.routes.api.MeetingsRepository"
        ) as repo_cls:
            instance = repo_cls.return_value
            instance.get_in_range = AsyncMock(return_value=[])

            resp = client.get("/api/meetings/upcoming")
            assert resp.status_code == 200
            data = resp.json()
            assert data == {"events": [], "poll_interval_seconds": 60}

    def test_single_meeting_field_mapping(self, client):
        """A single in-window meeting maps to the wire schema correctly."""
        start = datetime(2026, 5, 3, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 3, 14, 30, tzinfo=timezone.utc)
        meeting = _make_meeting(
            calendar_event_id="evt-abc",
            title="Q3 Planning",
            start_time=start,
            end_time=end,
            online_meeting_url="https://teams.example/abc",
            attendees=[object(), object(), object()],
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 1
        ev = events[0]
        assert ev["calendar_event_id"] == "evt-abc"
        assert ev["title"] == "Q3 Planning"
        assert ev["starts_at"] == start.timestamp()
        assert ev["ends_at"] == end.timestamp()
        assert ev["is_online_meeting"] is True
        assert ev["is_excluded"] is False
        assert ev["exclusion_reason"] is None
        assert ev["series_master_id"] is None
        assert ev["attendee_count"] == 3

    def test_keyword_excluded_meeting(self, client):
        """helios_exclude=None + is_excluded=True → keyword_match."""
        meeting = _make_meeting(
            title="Confidential Review",
            is_excluded=True,
            helios_exclude=None,
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        ev = resp.json()["events"][0]
        assert ev["title"] == "(excluded)"
        assert ev["is_excluded"] is True
        assert ev["exclusion_reason"] == "keyword_match"

    def test_helios_opt_out_meeting(self, client):
        """helios_exclude=True wins over keyword reason."""
        meeting = _make_meeting(
            title="1:1 with manager",
            is_excluded=False,
            helios_exclude=True,
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        ev = resp.json()["events"][0]
        assert ev["title"] == "(excluded)"
        assert ev["is_excluded"] is True
        assert ev["exclusion_reason"] == "helios_opt_out"

    def test_helios_opt_out_overrides_keyword(self, client):
        """helios_exclude=True wins even when keyword would also match."""
        meeting = _make_meeting(
            title="Confidential 1:1",
            is_excluded=True,        # would match keyword
            helios_exclude=True,     # but user explicit opt-out wins
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        ev = resp.json()["events"][0]
        assert ev["is_excluded"] is True
        assert ev["exclusion_reason"] == "helios_opt_out"

    def test_helios_override_false_unblocks_keyword_match(self, client):
        """helios_exclude=False forces inclusion even when keyword matches.

        Tri-state Option C: an explicit False is a user override that says
        "always include this meeting" regardless of keyword exclusion.
        """
        meeting = _make_meeting(
            title="Confidential Review",
            is_excluded=True,         # keyword would exclude
            helios_exclude=False,     # but user explicit override wins
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        ev = resp.json()["events"][0]
        # Title is the real title — meeting is NOT excluded.
        assert ev["title"] == "Confidential Review"
        assert ev["is_excluded"] is False
        assert ev["exclusion_reason"] is None

    def test_helios_exclude_null_falls_through_to_keyword(self, client):
        """helios_exclude=None defers to the keyword/manual is_excluded flag.

        This is the default state for every existing row; the tri-state
        column ships nullable so meetings that haven't been explicitly
        toggled from the dashboard continue to honor keyword exclusion.
        """
        keyword_hit = _make_meeting(
            title="HR Performance Review",
            is_excluded=True,
            helios_exclude=None,
        )
        no_keyword = _make_meeting(
            title="Project Sync",
            is_excluded=False,
            helios_exclude=None,
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(
                return_value=[keyword_hit, no_keyword]
            )
            resp = client.get("/api/meetings/upcoming")

        events = resp.json()["events"]
        assert events[0]["is_excluded"] is True
        assert events[0]["exclusion_reason"] == "keyword_match"
        assert events[1]["is_excluded"] is False
        assert events[1]["exclusion_reason"] is None

    def test_no_attendees_not_excluded(self, client):
        """Empty attendees is NOT an exclusion signal (HELIOS.md §16.5/§16.6).

        meeting_attendees rows are populated lazily by a separate
        calendar-sync code path; treating empty as "exclude" silently
        skipped real meetings during partial backfills.
        """
        meeting = _make_meeting(
            title="Focus Time",
            attendees=[],
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        ev = resp.json()["events"][0]
        assert ev["title"] == "Focus Time"
        assert ev["is_excluded"] is False
        assert ev["exclusion_reason"] is None
        assert ev["attendee_count"] == 0

    def test_get_in_range_called_with_now_and_horizon(self, client):
        """The repo receives [now_utc, now_utc+horizon_minutes]."""
        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            instance = repo_cls.return_value
            instance.get_in_range = AsyncMock(return_value=[])
            before = datetime.now(timezone.utc)

            resp = client.get("/api/meetings/upcoming?horizon_minutes=120")
            assert resp.status_code == 200

            after = datetime.now(timezone.utc)
            instance.get_in_range.assert_awaited_once()
            start_arg, end_arg = instance.get_in_range.await_args.args

            # start_arg is "now-ish"; should sit between before/after
            assert before <= start_arg <= after
            # end_arg = start_arg + 120 minutes
            delta = end_arg - start_arg
            assert delta == timedelta(minutes=120)

    def test_recurring_instance_includes_series_master(self, client):
        """recurring_series_id is surfaced as series_master_id."""
        meeting = _make_meeting(
            calendar_event_id="evt-recur-3",
            recurring_series_id="series-root-xyz",
        )

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        ev = resp.json()["events"][0]
        assert ev["series_master_id"] == "series-root-xyz"

    def test_horizon_validation_zero_rejected(self, client):
        resp = client.get("/api/meetings/upcoming?horizon_minutes=0")
        assert resp.status_code == 422

    def test_horizon_validation_too_large_rejected(self, client):
        resp = client.get("/api/meetings/upcoming?horizon_minutes=1441")
        assert resp.status_code == 422

    @pytest.mark.parametrize("h", [1, 60, 1440])
    def test_horizon_validation_valid_values(self, client, h):
        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[])
            resp = client.get(f"/api/meetings/upcoming?horizon_minutes={h}")
        assert resp.status_code == 200

    def test_default_horizon_is_60_minutes(self, client):
        """Omitting horizon_minutes defaults to a 60-minute window."""
        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            instance = repo_cls.return_value
            instance.get_in_range = AsyncMock(return_value=[])

            resp = client.get("/api/meetings/upcoming")
            assert resp.status_code == 200

            start_arg, end_arg = instance.get_in_range.await_args.args
            assert (end_arg - start_arg) == timedelta(minutes=60)

    def test_offline_meeting_flag(self, client):
        """No online_meeting_url → is_online_meeting=False."""
        meeting = _make_meeting(online_meeting_url=None)

        with patch("aegis.web.routes.api.MeetingsRepository") as repo_cls:
            repo_cls.return_value.get_in_range = AsyncMock(return_value=[meeting])
            resp = client.get("/api/meetings/upcoming")

        assert resp.json()["events"][0]["is_online_meeting"] is False
