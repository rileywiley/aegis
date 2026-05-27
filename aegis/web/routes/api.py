"""JSON API surface consumed by Helios (and future external integrations).

Per HELIOS.md §16.5 / §16.6. Currently exposes one endpoint:

    GET /api/meetings/upcoming?horizon_minutes=N

No authentication — loopback only, consistent with Aegis's other read endpoints.

The exclusion contract honors two signals:
  * ``helios_exclude`` (tri-state, per HELIOS_BUILD_PLAN.md L2210-2216 Option C):
      - ``True``  → always exclude (user explicit opt-out, wins over keywords)
      - ``False`` → always include (user explicit override, wins over keywords)
      - ``None``  → fall through to keyword/manual logic
  * ``is_excluded`` (Aegis's keyword-based exclusion flag — §16.6) — only
    consulted when ``helios_exclude IS NULL``.

Attendee-count is intentionally NOT an exclusion signal: meeting_attendees
rows are populated by a separate calendar-sync code path and are missing
during partial backfills. Treating empty attendees as "exclude" caused
Helios to silently skip captures for legitimate meetings.

When excluded, the response title is replaced with "(excluded)" so Helios never
sees PII for meetings the user has chosen to omit from capture.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.engine import get_session
from aegis.db.models import Meeting
from aegis.db.repositories import MeetingsRepository
from shared.meetings import UpcomingMeetingEvent, UpcomingMeetingsResponse

router = APIRouter()


def _exclusion_reason(meeting: Meeting) -> str | None:
    """First matching exclusion reason, or None if the meeting is not excluded.

    Tri-state precedence (HELIOS_BUILD_PLAN.md L2210-2216 Option C):
      1. ``helios_exclude=True``  → ``"helios_opt_out"`` (wins over keywords)
      2. ``helios_exclude=False`` → not excluded (overrides keyword match)
      3. ``helios_exclude=None``  → fall back to keyword/manual logic
    Attendee count is NOT a reason — see module docstring.
    """
    helios_exclude = getattr(meeting, "helios_exclude", None)
    if helios_exclude is True:
        return "helios_opt_out"
    if helios_exclude is False:
        # Explicit user override of the keyword exclusion.
        return None
    # helios_exclude IS NULL → defer to keyword/manual flag.
    if meeting.is_excluded:
        return "keyword_match"
    return None


def _effective_excluded(meeting: Meeting) -> bool:
    """True if any exclusion condition applies."""
    return _exclusion_reason(meeting) is not None


@router.get("/api/meetings/upcoming")
async def meetings_upcoming(
    horizon_minutes: int = Query(60, ge=1, le=1440),
    session: AsyncSession = Depends(get_session),
) -> UpcomingMeetingsResponse:
    """Return Aegis-known meetings starting between now and now+horizon_minutes.

    Helios polls this endpoint to pre-arm captures. See HELIOS.md §16.5 for the
    contract; `shared.meetings` defines the wire format.
    """
    repo = MeetingsRepository(session)
    now = datetime.now(timezone.utc)
    until = now + timedelta(minutes=horizon_minutes)
    # NOTE: relies on MeetingsRepository.get_in_range eager-loading
    # ``Meeting.attendees`` via ``selectinload``. Accessing
    # ``m.attendees`` from this async route would trigger lazy I/O and
    # raise ``MissingGreenlet`` if the repo ever stops eager-loading.
    meetings = await repo.get_in_range(now, until)

    events: list[UpcomingMeetingEvent] = []
    for m in meetings:
        excluded = _effective_excluded(m)
        events.append(
            UpcomingMeetingEvent(
                calendar_event_id=m.calendar_event_id or "",
                title="(excluded)" if excluded else m.title,
                starts_at=m.start_time.timestamp(),
                ends_at=m.end_time.timestamp(),
                is_online_meeting=bool(m.online_meeting_url),
                is_excluded=excluded,
                exclusion_reason=_exclusion_reason(m),
                series_master_id=m.recurring_series_id,
                attendee_count=len(m.attendees),
            )
        )

    return UpcomingMeetingsResponse(events=events)
