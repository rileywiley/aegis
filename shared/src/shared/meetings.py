"""Aegis → Helios meeting schedule contract. Matches HELIOS.md §16.5."""

from pydantic import BaseModel


class UpcomingMeetingEvent(BaseModel):
    calendar_event_id: str
    title: str  # "(excluded)" when is_excluded=True
    starts_at: float  # UTC epoch seconds
    ends_at: float    # UTC epoch seconds
    is_online_meeting: bool = False
    is_excluded: bool = False
    exclusion_reason: str | None = None
    series_master_id: str | None = None
    attendee_count: int = 0


class UpcomingMeetingsResponse(BaseModel):
    events: list[UpcomingMeetingEvent]
    poll_interval_seconds: int = 60
