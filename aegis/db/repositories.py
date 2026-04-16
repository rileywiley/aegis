"""Data access layer — query patterns for Phase 1+."""

from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import (
    ActionItem,
    Commitment,
    Decision,
    Meeting,
    MeetingAttendee,
    MeetingTopic,
    Person,
    SystemHealth,
    Topic,
    Workstream,
    WorkstreamItem,
    WorkstreamMilestone,
    WorkstreamStakeholder,
)


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


async def get_person_by_id(session: AsyncSession, person_id: int) -> Person | None:
    """Fetch a person by ID."""
    return await session.get(Person, person_id)


async def get_persons_by_ids(
    session: AsyncSession, person_ids: list[int]
) -> dict[int, Person]:
    """Fetch multiple persons by their IDs. Returns a dict of id -> Person."""
    if not person_ids:
        return {}
    stmt = select(Person).where(Person.id.in_(person_ids))
    result = await session.execute(stmt)
    return {p.id: p for p in result.scalars().all()}


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


# ── Extracted Entities (Phase 2) ────────────────────────


async def create_action_item(session: AsyncSession, **kwargs) -> ActionItem:
    """Create an action item record."""
    item = ActionItem(**kwargs)
    session.add(item)
    await session.flush()
    return item


async def create_decision(session: AsyncSession, **kwargs) -> Decision:
    """Create a decision record."""
    item = Decision(**kwargs)
    session.add(item)
    await session.flush()
    return item


async def create_commitment(session: AsyncSession, **kwargs) -> Commitment:
    """Create a commitment record."""
    item = Commitment(**kwargs)
    session.add(item)
    await session.flush()
    return item


async def upsert_topic(session: AsyncSession, name: str) -> Topic:
    """Get existing topic by name or create a new one."""
    stmt = select(Topic).where(Topic.name == name)
    result = await session.execute(stmt)
    topic = result.scalar_one_or_none()
    if topic:
        return topic
    topic = Topic(name=name)
    session.add(topic)
    await session.flush()
    return topic


async def link_meeting_topics(
    session: AsyncSession, meeting_id: int, topic_ids: list[int]
) -> None:
    """Link topics to a meeting (idempotent — uses INSERT ON CONFLICT DO NOTHING)."""
    for topic_id in topic_ids:
        stmt = pg_insert(MeetingTopic).values(
            meeting_id=meeting_id, topic_id=topic_id
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["meeting_id", "topic_id"]
        )
        await session.execute(stmt)


async def get_all_people(session: AsyncSession) -> list[Person]:
    """Fetch all people — used by resolver for fuzzy matching."""
    stmt = select(Person)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_meeting_extraction(
    session: AsyncSession,
    meeting_id: int,
    summary: str,
    sentiment: str,
    embedding: list[float],
) -> None:
    """Update a meeting with extraction results."""
    from datetime import timezone

    stmt = (
        update(Meeting)
        .where(Meeting.id == meeting_id)
        .values(
            summary=summary,
            sentiment=sentiment,
            embedding=embedding,
            last_extracted_at=datetime.now(timezone.utc),
        )
    )
    await session.execute(stmt)


# ── Workstreams ─────────────────────────────────────────


async def create_workstream(
    session: AsyncSession,
    name: str,
    description: str | None = None,
    owner_id: int | None = None,
    status: str = "active",
    target_date: date | None = None,
    created_by: str = "manual",
    confidence: float = 1.0,
    pinned: bool = False,
) -> Workstream:
    """Create a new workstream."""
    ws = Workstream(
        name=name,
        description=description,
        owner_id=owner_id,
        status=status,
        target_date=target_date,
        created_by=created_by,
        confidence=confidence,
        pinned=pinned,
        is_managed=owner_id is not None,
    )
    session.add(ws)
    await session.commit()
    await session.refresh(ws)
    return ws


async def update_workstream(
    session: AsyncSession, workstream_id: int, **kwargs: object
) -> Workstream | None:
    """Update workstream fields. Returns updated workstream or None if not found."""
    ws = await session.get(Workstream, workstream_id)
    if ws is None:
        return None
    for key, value in kwargs.items():
        if hasattr(ws, key):
            setattr(ws, key, value)
    ws.updated = datetime.utcnow()
    await session.commit()
    await session.refresh(ws)
    return ws


async def get_workstreams(
    session: AsyncSession,
    status_filter: str | None = None,
    search: str | None = None,
) -> list[Workstream]:
    """Fetch workstreams. Pinned first, then by updated desc."""
    stmt = select(Workstream).order_by(
        Workstream.pinned.desc(), Workstream.updated.desc()
    )
    if status_filter:
        stmt = stmt.where(Workstream.status == status_filter)
    if search:
        stmt = stmt.where(Workstream.name.ilike(f"%{search}%"))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_workstream_by_id(
    session: AsyncSession, workstream_id: int
) -> Workstream | None:
    return await session.get(Workstream, workstream_id)


async def get_workstream_items(
    session: AsyncSession, workstream_id: int
) -> list[WorkstreamItem]:
    """Fetch all items linked to a workstream, ordered by linked_at desc."""
    stmt = (
        select(WorkstreamItem)
        .where(WorkstreamItem.workstream_id == workstream_id)
        .order_by(WorkstreamItem.linked_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_workstream_stakeholders(
    session: AsyncSession, workstream_id: int
) -> list[dict]:
    """Fetch stakeholders for a workstream as dicts with person info + role."""
    stmt = (
        select(
            Person.id,
            Person.name,
            Person.email,
            Person.title,
            WorkstreamStakeholder.role,
        )
        .join(WorkstreamStakeholder, WorkstreamStakeholder.person_id == Person.id)
        .where(WorkstreamStakeholder.workstream_id == workstream_id)
    )
    result = await session.execute(stmt)
    return [
        {
            "id": row.id,
            "name": row.name,
            "email": row.email,
            "title": row.title,
            "role": row.role,
        }
        for row in result.all()
    ]


async def get_workstream_milestones(
    session: AsyncSession, workstream_id: int
) -> list[WorkstreamMilestone]:
    """Fetch milestones for a workstream, ordered by target_date."""
    stmt = (
        select(WorkstreamMilestone)
        .where(WorkstreamMilestone.workstream_id == workstream_id)
        .order_by(WorkstreamMilestone.target_date.asc().nullslast())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_workstream_item_counts(
    session: AsyncSession, workstream_ids: list[int]
) -> dict[int, int]:
    """Return a map of workstream_id -> count of linked items."""
    if not workstream_ids:
        return {}
    stmt = (
        select(WorkstreamItem.workstream_id, func.count())
        .where(WorkstreamItem.workstream_id.in_(workstream_ids))
        .group_by(WorkstreamItem.workstream_id)
    )
    result = await session.execute(stmt)
    return {ws_id: cnt for ws_id, cnt in result.all()}


async def get_workstream_owner_names(
    session: AsyncSession, owner_ids: list[int]
) -> dict[int, str]:
    """Return a map of person_id -> name for the given owner IDs."""
    if not owner_ids:
        return {}
    stmt = select(Person.id, Person.name).where(Person.id.in_(owner_ids))
    result = await session.execute(stmt)
    return {pid: name for pid, name in result.all()}


# ── Workstream Item Linking ─────────────────────────────


async def link_item_to_workstream(
    session: AsyncSession,
    workstream_id: int,
    item_type: str,
    item_id: int,
    linked_by: str = "manual",
    relevance_score: float = 1.0,
) -> None:
    """Link an item to a workstream. Idempotent via UNIQUE constraint."""
    stmt = pg_insert(WorkstreamItem).values(
        workstream_id=workstream_id,
        item_type=item_type,
        item_id=item_id,
        linked_by=linked_by,
        relevance_score=relevance_score,
    )
    stmt = stmt.on_conflict_do_nothing(
        constraint="uq_workstream_item",
    )
    await session.execute(stmt)
    await session.commit()


async def unlink_item_from_workstream(
    session: AsyncSession,
    workstream_id: int,
    item_type: str,
    item_id: int,
) -> None:
    """Remove a linked item from a workstream."""
    stmt = select(WorkstreamItem).where(
        WorkstreamItem.workstream_id == workstream_id,
        WorkstreamItem.item_type == item_type,
        WorkstreamItem.item_id == item_id,
    )
    result = await session.execute(stmt)
    item = result.scalar_one_or_none()
    if item:
        await session.delete(item)
        await session.commit()


# ── Action Items ────────────────────────────────────────


async def get_action_items(
    session: AsyncSession,
    status: str | None = None,
    assignee_id: int | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[ActionItem], int]:
    """Fetch action items with filters and pagination. Returns (items, total_count)."""
    stmt = select(ActionItem).order_by(ActionItem.created.desc())

    if status:
        stmt = stmt.where(ActionItem.status == status)
    if assignee_id:
        stmt = stmt.where(ActionItem.assignee_id == assignee_id)
    if search:
        stmt = stmt.where(ActionItem.description.ilike(f"%{search}%"))

    # Total count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    # Paginate
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await session.execute(stmt)
    items = list(result.scalars().all())

    return items, total


async def update_action_item_status(
    session: AsyncSession, action_item_id: int, new_status: str
) -> None:
    """Update the status of an action item."""
    stmt = (
        update(ActionItem)
        .where(ActionItem.id == action_item_id)
        .values(status=new_status, updated=datetime.utcnow())
    )
    await session.execute(stmt)
    await session.commit()
