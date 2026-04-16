"""Data access layer — query patterns for Phase 1+."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import Meeting, MeetingAttendee, Person, SystemHealth


# ── Meetings ─────────────────────────────────────────────


async def upsert_meeting(session: AsyncSession, data: dict) -> Meeting:
    """Insert or update a meeting by calendar_event_id (idempotent)."""
    stmt = pg_insert(Meeting).values(**data)
    stmt = stmt.on_conflict_do_update(
        index_elements=["calendar_event_id"],
        set_={k: v for k, v in data.items() if k != "calendar_event_id"},
    )
    stmt = stmt.returning(Meeting.__table__.c.id)
    result = await session.execute(stmt)
    meeting_id = result.scalar_one()
    await session.commit()
    row = await session.get(Meeting, meeting_id)
    return row


async def get_meetings_for_range(
    session: AsyncSession, start: datetime, end: datetime
) -> list[Meeting]:
    """Fetch meetings within a time range, ordered by start_time."""
    stmt = (
        select(Meeting)
        .where(Meeting.start_time >= start, Meeting.start_time < end)
        .order_by(Meeting.start_time)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_meeting_by_id(session: AsyncSession, meeting_id: int) -> Meeting | None:
    return await session.get(Meeting, meeting_id)


async def get_meeting_attendees(
    session: AsyncSession, meeting_id: int
) -> list[Person]:
    stmt = (
        select(Person)
        .join(MeetingAttendee, MeetingAttendee.person_id == Person.id)
        .where(MeetingAttendee.meeting_id == meeting_id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_meeting_transcript(
    session: AsyncSession,
    meeting_id: int,
    transcript_text: str,
    transcript_status: str,
) -> None:
    stmt = (
        update(Meeting)
        .where(Meeting.id == meeting_id)
        .values(transcript_text=transcript_text, transcript_status=transcript_status)
    )
    await session.execute(stmt)
    await session.commit()


async def set_meeting_excluded(
    session: AsyncSession, meeting_id: int, excluded: bool
) -> None:
    stmt = (
        update(Meeting)
        .where(Meeting.id == meeting_id)
        .values(is_excluded=excluded)
    )
    await session.execute(stmt)
    await session.commit()


# ── People ───────────────────────────────────────────────


async def get_or_create_person_by_email(
    session: AsyncSession, email: str, name: str, source: str = "calendar"
) -> Person:
    """Find person by email or create stub record."""
    stmt = select(Person).where(Person.email == email)
    result = await session.execute(stmt)
    person = result.scalar_one_or_none()
    if person:
        return person
    person = Person(name=name, email=email, source=source, needs_review=True)
    session.add(person)
    await session.flush()
    return person


# ── System Health ────────────────────────────────────────


async def upsert_system_health(
    session: AsyncSession,
    service: str,
    *,
    status: str = "healthy",
    last_success: datetime | None = None,
    last_error: datetime | None = None,
    last_error_message: str | None = None,
    items_processed: int | None = None,
) -> None:
    data: dict = {"service": service, "status": status, "updated": datetime.utcnow()}
    if last_success:
        data["last_success"] = last_success
    if last_error:
        data["last_error"] = last_error
    if last_error_message:
        data["last_error_message"] = last_error_message
    if items_processed is not None:
        data["items_processed_last_hour"] = items_processed

    stmt = pg_insert(SystemHealth).values(**data)
    stmt = stmt.on_conflict_do_update(
        index_elements=["service"],
        set_={k: v for k, v in data.items() if k != "service"},
    )
    await session.execute(stmt)
    await session.commit()


# ── Crash Recovery ───────────────────────────────────────


async def reset_stuck_processing(session: AsyncSession) -> int:
    """Reset items stuck in 'processing' back to 'pending'. Returns count."""
    count = 0
    for model in [Meeting]:  # extend with Email, ChatMessage in Phase 3
        stmt = (
            update(model)
            .where(model.processing_status == "processing")
            .values(processing_status="pending")
        )
        result = await session.execute(stmt)
        count += result.rowcount
    await session.commit()
    return count
