"""Data access layer — query patterns for Phase 1+."""

from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import (
    ActionItem,
    ChatAsk,
    ChatMessage,
    ChatMessageTopic,
    Commitment,
    Decision,
    Department,
    Email,
    EmailAsk,
    EmailTopic,
    Meeting,
    MeetingAttendee,
    MeetingTopic,
    Person,
    SystemHealth,
    Team,
    TeamChannel,
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
    for model in [Meeting, Email, ChatMessage]:
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


# ── Teams & Chat Messages (Phase 3) ──────────────────────


async def get_chat_message_by_id(
    session: AsyncSession, message_id: int
) -> ChatMessage | None:
    """Fetch a chat message by ID."""
    return await session.get(ChatMessage, message_id)


async def get_chat_messages_for_channel(
    session: AsyncSession,
    channel_id: int,
    since: datetime | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[ChatMessage], int]:
    """Fetch chat messages for a channel with pagination."""
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.channel_id == channel_id)
        .order_by(ChatMessage.datetime_.desc())
    )
    if since:
        stmt = stmt.where(ChatMessage.datetime_ >= since)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def get_pending_chat_messages(
    session: AsyncSession, limit: int = 100
) -> list[ChatMessage]:
    """Fetch chat messages with processing_status='pending' (non-noise)."""
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.processing_status == "pending",
            ChatMessage.noise_filtered == False,  # noqa: E712
        )
        .order_by(ChatMessage.datetime_)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_chat_asks(
    session: AsyncSession,
    status: str | None = None,
    target_id: int | None = None,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[ChatAsk], int]:
    """Fetch chat asks with filters and pagination."""
    stmt = select(ChatAsk).order_by(ChatAsk.created.desc())
    if status:
        stmt = stmt.where(ChatAsk.status == status)
    if target_id:
        stmt = stmt.where(ChatAsk.target_id == target_id)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def get_chat_messages_for_meeting(
    session: AsyncSession, meeting_id: int
) -> list[ChatMessage]:
    """Fetch Teams chat messages linked to a specific meeting."""
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.linked_meeting_id == meeting_id)
        .order_by(ChatMessage.datetime_)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_teams_list(session: AsyncSession) -> list[Team]:
    """Fetch all teams."""
    stmt = select(Team).order_by(Team.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_team_channels_list(
    session: AsyncSession, team_id: int
) -> list[TeamChannel]:
    """Fetch channels for a team."""
    stmt = (
        select(TeamChannel)
        .where(TeamChannel.team_id == team_id)
        .order_by(TeamChannel.name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def link_chat_message_topics(
    session: AsyncSession, message_id: int, topic_ids: list[int]
) -> None:
    """Link topics to a chat message (idempotent)."""
    for topic_id in topic_ids:
        stmt = pg_insert(ChatMessageTopic).values(
            chat_message_id=message_id, topic_id=topic_id
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["chat_message_id", "topic_id"]
        )
        await session.execute(stmt)


# ── Emails ──────────────────────────────────────────────


async def get_emails(
    session: AsyncSession,
    email_class: str | None = None,
    intent: str | None = None,
    triage_class: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[Email], int]:
    """Fetch emails with filters and pagination. Returns (emails, total_count)."""
    stmt = select(Email).order_by(Email.datetime_.desc())

    if email_class:
        stmt = stmt.where(Email.email_class == email_class)
    if intent:
        stmt = stmt.where(Email.intent == intent)
    if triage_class:
        stmt = stmt.where(Email.triage_class == triage_class)
    if search:
        stmt = stmt.where(Email.subject.ilike(f"%{search}%"))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await session.execute(stmt)
    items = list(result.scalars().all())

    return items, total


async def get_email_by_id(session: AsyncSession, email_id: int) -> Email | None:
    """Fetch an email by ID."""
    return await session.get(Email, email_id)


async def get_email_asks_for_email(
    session: AsyncSession, email_id: int
) -> list[EmailAsk]:
    """Fetch all asks associated with an email."""
    stmt = (
        select(EmailAsk)
        .where(EmailAsk.email_id == email_id)
        .order_by(EmailAsk.created.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_email_extraction(
    session: AsyncSession,
    email_id: int,
    summary: str,
    intent: str,
    requires_response: bool,
    sentiment: str,
    embedding: list[float],
) -> None:
    """Update an email with extraction results."""
    from datetime import timezone

    stmt = (
        update(Email)
        .where(Email.id == email_id)
        .values(
            summary=summary,
            intent=intent,
            requires_response=requires_response,
            sentiment=sentiment,
            embedding=embedding,
            last_extracted_at=datetime.now(timezone.utc),
            processing_status="completed",
        )
    )
    await session.execute(stmt)


async def link_email_topics(
    session: AsyncSession, email_id: int, topic_ids: list[int]
) -> None:
    """Link topics to an email (idempotent)."""
    for topic_id in topic_ids:
        stmt = pg_insert(EmailTopic).values(
            email_id=email_id, topic_id=topic_id
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["email_id", "topic_id"]
        )
        await session.execute(stmt)


# ── Asks (Email + Chat combined) ───────────────────────


async def get_all_asks(
    session: AsyncSession,
    status: str | None = None,
    urgency: str | None = None,
    ask_type: str | None = None,
    source: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[dict], int]:
    """Fetch combined email_asks + chat_asks with filters and pagination.

    Returns (list of ask dicts with source info, total_count).
    source: 'email', 'chat', or None (both).
    """
    include_email = source in (None, "email")
    include_chat = source in (None, "chat")

    ea_stmt = select(EmailAsk).order_by(EmailAsk.created.desc())
    ca_stmt = select(ChatAsk).order_by(ChatAsk.created.desc())

    if status:
        ea_stmt = ea_stmt.where(EmailAsk.status == status)
        ca_stmt = ca_stmt.where(ChatAsk.status == status)
    if urgency:
        ea_stmt = ea_stmt.where(EmailAsk.urgency == urgency)
        ca_stmt = ca_stmt.where(ChatAsk.urgency == urgency)
    if ask_type:
        ea_stmt = ea_stmt.where(EmailAsk.ask_type == ask_type)
        ca_stmt = ca_stmt.where(ChatAsk.ask_type == ask_type)

    ea_count = 0
    ca_count = 0
    email_asks: list = []
    chat_asks: list = []

    if include_email:
        ea_count = (
            await session.execute(select(func.count()).select_from(ea_stmt.subquery()))
        ).scalar() or 0
        ea_result = await session.execute(ea_stmt)
        email_asks = list(ea_result.scalars().all())

    if include_chat:
        ca_count = (
            await session.execute(select(func.count()).select_from(ca_stmt.subquery()))
        ).scalar() or 0
        ca_result = await session.execute(ca_stmt)
        chat_asks = list(ca_result.scalars().all())

    total = ea_count + ca_count

    combined: list[dict] = []
    for ea in email_asks:
        combined.append({
            "id": ea.id,
            "description": ea.description,
            "ask_type": ea.ask_type,
            "requester_id": ea.requester_id,
            "target_id": ea.target_id,
            "urgency": ea.urgency,
            "status": ea.status,
            "deadline": ea.deadline,
            "created": ea.created,
            "source_type": "email",
            "source_id": ea.email_id,
        })
    for ca in chat_asks:
        combined.append({
            "id": ca.id,
            "description": ca.description,
            "ask_type": ca.ask_type,
            "requester_id": ca.requester_id,
            "target_id": ca.target_id,
            "urgency": ca.urgency,
            "status": ca.status,
            "deadline": ca.deadline,
            "created": ca.created,
            "source_type": "chat",
            "source_id": ca.message_id,
        })

    combined.sort(key=lambda x: x["created"] or datetime.min, reverse=True)

    start = (page - 1) * per_page
    end = start + per_page
    page_items = combined[start:end]

    return page_items, total


async def update_email_ask_status(
    session: AsyncSession, ask_id: int, new_status: str
) -> None:
    """Update the status of an email ask."""
    stmt = (
        update(EmailAsk)
        .where(EmailAsk.id == ask_id)
        .values(status=new_status, updated=datetime.utcnow())
    )
    await session.execute(stmt)
    await session.commit()


async def update_chat_ask_status(
    session: AsyncSession, ask_id: int, new_status: str
) -> None:
    """Update the status of a chat ask."""
    stmt = (
        update(ChatAsk)
        .where(ChatAsk.id == ask_id)
        .values(status=new_status, updated=datetime.utcnow())
    )
    await session.execute(stmt)
    await session.commit()


# ── Departments ────────────────────────────────────────


async def get_departments(session: AsyncSession) -> list[Department]:
    """Fetch all departments ordered by name."""
    stmt = select(Department).order_by(Department.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_department_by_id(
    session: AsyncSession, dept_id: int
) -> Department | None:
    """Fetch a single department by ID."""
    return await session.get(Department, dept_id)


async def get_department_members(
    session: AsyncSession, dept_id: int
) -> list[Person]:
    """Fetch all people belonging to a department."""
    stmt = (
        select(Person)
        .where(Person.department_id == dept_id)
        .order_by(Person.name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_department_member_count(
    session: AsyncSession, dept_id: int
) -> int:
    """Count members in a department."""
    stmt = (
        select(func.count())
        .select_from(Person)
        .where(Person.department_id == dept_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_department_open_items(
    session: AsyncSession, dept_id: int
) -> dict:
    """Count open action_items + email_asks + chat_asks for department members.

    Returns dict with keys: open_action_items, open_email_asks, open_chat_asks,
    overdue_action_items, total_open, total_overdue
    """
    member_ids_stmt = select(Person.id).where(Person.department_id == dept_id)
    member_result = await session.execute(member_ids_stmt)
    member_ids = [row for row in member_result.scalars().all()]

    if not member_ids:
        return {
            "open_action_items": 0,
            "open_email_asks": 0,
            "open_chat_asks": 0,
            "overdue_action_items": 0,
            "total_open": 0,
            "total_overdue": 0,
        }

    ai_stmt = (
        select(func.count())
        .select_from(ActionItem)
        .where(
            ActionItem.assignee_id.in_(member_ids),
            ActionItem.status.in_(["open", "in_progress"]),
        )
    )
    open_action_items = (await session.execute(ai_stmt)).scalar_one()

    stale_ai_stmt = (
        select(func.count())
        .select_from(ActionItem)
        .where(
            ActionItem.assignee_id.in_(member_ids),
            ActionItem.status == "stale",
        )
    )
    overdue_action_items = (await session.execute(stale_ai_stmt)).scalar_one()

    ea_stmt = (
        select(func.count())
        .select_from(EmailAsk)
        .where(
            EmailAsk.target_id.in_(member_ids),
            EmailAsk.status.in_(["open", "in_progress"]),
        )
    )
    open_email_asks = (await session.execute(ea_stmt)).scalar_one()

    ca_stmt = (
        select(func.count())
        .select_from(ChatAsk)
        .where(
            ChatAsk.target_id.in_(member_ids),
            ChatAsk.status.in_(["open", "in_progress"]),
        )
    )
    open_chat_asks = (await session.execute(ca_stmt)).scalar_one()

    total_open = open_action_items + open_email_asks + open_chat_asks

    return {
        "open_action_items": open_action_items,
        "open_email_asks": open_email_asks,
        "open_chat_asks": open_chat_asks,
        "overdue_action_items": overdue_action_items,
        "total_open": total_open,
        "total_overdue": overdue_action_items,
    }


async def get_department_workstreams(
    session: AsyncSession, dept_id: int
) -> list[Workstream]:
    """Fetch active workstreams that involve department members as stakeholders."""
    member_ids_stmt = select(Person.id).where(Person.department_id == dept_id)
    member_result = await session.execute(member_ids_stmt)
    member_ids = [row for row in member_result.scalars().all()]

    if not member_ids:
        return []

    stmt = (
        select(Workstream)
        .join(
            WorkstreamStakeholder,
            WorkstreamStakeholder.workstream_id == Workstream.id,
        )
        .where(
            WorkstreamStakeholder.person_id.in_(member_ids),
            Workstream.status.in_(["active", "quiet"]),
        )
        .distinct()
        .order_by(Workstream.updated.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
