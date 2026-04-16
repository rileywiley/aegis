"""Calendar sync — fetch events from Graph API, filter, and upsert into meetings table."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import MeetingAttendee
from aegis.db.repositories import get_or_create_person_by_email, upsert_meeting
from aegis.ingestion.graph_client import GraphClient

logger = logging.getLogger(__name__)


class CalendarSync:
    """Pulls calendar events from Microsoft Graph and upserts them as meetings."""

    def __init__(self, graph_client: GraphClient) -> None:
        self._graph = graph_client

    async def sync(self, session: AsyncSession) -> int:
        """Sync today + tomorrow calendar events. Returns count of upserted meetings."""
        settings = get_settings()

        now_utc = datetime.now(timezone.utc)
        start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_tomorrow = start_of_today + timedelta(days=2)

        start_str = start_of_today.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_of_tomorrow.strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info("Calendar sync: fetching events from %s to %s", start_str, end_str)
        events = await self._graph.get_calendar_events(start_str, end_str)
        logger.info("Calendar sync: received %d raw events", len(events))

        exclusion_keywords = [kw.lower() for kw in settings.exclusion_keywords_list]
        upserted_count = 0

        for event in events:
            if self._should_skip(event, exclusion_keywords):
                continue

            meeting_data = self._event_to_meeting_data(event)
            meeting = await upsert_meeting(session, meeting_data)
            upserted_count += 1

            # Extract attendees and create person stubs + junction records
            attendees = event.get("attendees", [])
            await self._sync_attendees(session, meeting.id, attendees)

        logger.info("Calendar sync: upserted %d meetings", upserted_count)
        return upserted_count

    def _should_skip(self, event: dict, exclusion_keywords: list[str]) -> bool:
        """Apply all filtering rules from the spec. Return True to skip."""

        # Skip all-day events
        if event.get("isAllDay"):
            logger.debug("Skipping all-day event: %s", event.get("subject", "?"))
            return True

        # Skip cancelled events
        if event.get("isCancelled"):
            logger.debug("Skipping cancelled event: %s", event.get("subject", "?"))
            return True

        # Skip declined events
        response_status = event.get("responseStatus", {})
        if response_status.get("response") == "declined":
            logger.debug("Skipping declined event: %s", event.get("subject", "?"))
            return True

        # Skip OOO / focus time (showAs: oof, free)
        show_as = (event.get("showAs") or "").lower()
        if show_as in ("oof", "free"):
            logger.debug("Skipping oof/free event: %s", event.get("subject", "?"))
            return True

        # Count real attendees (excluding organizer)
        attendees = event.get("attendees", [])
        attendee_count = len(attendees)

        is_online = event.get("isOnlineMeeting", False)

        # Skip solo blocks: <=1 attendee AND not an online meeting
        if attendee_count <= 1 and not is_online:
            logger.debug("Skipping solo block: %s", event.get("subject", "?"))
            return True

        # Keyword exclusion (case-insensitive partial match on subject)
        subject = (event.get("subject") or "").lower()
        for keyword in exclusion_keywords:
            if keyword in subject:
                logger.debug(
                    "Skipping excluded keyword '%s' in: %s", keyword, event.get("subject", "?")
                )
                return True

        return False

    def _event_to_meeting_data(self, event: dict) -> dict:
        """Convert a Graph API event dict to a meetings table row dict."""

        start_dt = _parse_graph_datetime(event["start"])
        end_dt = _parse_graph_datetime(event["end"])
        duration_minutes = int((end_dt - start_dt).total_seconds() / 60)

        # Determine meeting type
        is_online = event.get("isOnlineMeeting", False)
        meeting_type = "virtual" if is_online else "in_person"

        # Online meeting URL
        online_url = event.get("onlineMeetingUrl") or None
        if not online_url:
            online_meeting = event.get("onlineMeeting") or {}
            online_url = online_meeting.get("joinUrl")

        # Organizer email
        organizer = event.get("organizer", {})
        organizer_email = organizer.get("emailAddress", {}).get("address")

        # Recurring series
        series_id = event.get("seriesMasterId")

        return {
            "title": event.get("subject") or "(No Subject)",
            "start_time": start_dt,
            "end_time": end_dt,
            "duration": duration_minutes,
            "status": "scheduled",
            "meeting_type": meeting_type,
            "calendar_event_id": event["id"],
            "online_meeting_url": online_url,
            "recurring_series_id": series_id,
            "organizer_email": organizer_email,
        }

    async def _sync_attendees(
        self, session: AsyncSession, meeting_id: int, attendees: list[dict]
    ) -> None:
        """Create person stubs and meeting_attendees junction records."""
        for att in attendees:
            email_info = att.get("emailAddress", {})
            email = email_info.get("address")
            name = email_info.get("name", "")
            if not email:
                continue

            person = await get_or_create_person_by_email(session, email=email, name=name)

            # Upsert junction record (ignore if already exists)
            stmt = pg_insert(MeetingAttendee).values(
                meeting_id=meeting_id, person_id=person.id
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["meeting_id", "person_id"]
            )
            await session.execute(stmt)

        await session.commit()


def _parse_graph_datetime(dt_obj: dict) -> datetime:
    """Parse Graph API datetime object {dateTime, timeZone} to UTC datetime.

    Graph returns dateTime as ISO string and timeZone (usually "UTC" for
    calendarView queries with UTC params).
    """
    raw = dt_obj["dateTime"]
    tz_name = dt_obj.get("timeZone", "UTC")

    # Graph returns ISO strings without offset when timeZone is UTC
    if tz_name == "UTC":
        # Strip trailing 'Z' if present, parse, and attach UTC tz
        raw = raw.rstrip("Z")
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=timezone.utc)

    # For non-UTC timezones, use dateutil to parse properly
    from dateutil import tz as dateutil_tz

    dt = datetime.fromisoformat(raw)
    source_tz = dateutil_tz.gettz(tz_name)
    if source_tz:
        dt = dt.replace(tzinfo=source_tz)
    return dt.astimezone(timezone.utc)
