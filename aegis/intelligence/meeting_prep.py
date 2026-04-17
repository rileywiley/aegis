"""Meeting prep brief generator — pre-computed context + talking points via Sonnet.

Generates attendee profiles, open items involving attendees, linked workstream status,
previous meeting in recurring series, and suggested talking points. Stored in the
briefings table with briefing_type='meeting_prep'.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from anthropic import AsyncAnthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import (
    ActionItem,
    Briefing,
    ChatAsk,
    ChatMessage,
    Email,
    EmailAsk,
    LLMUsage,
    Meeting,
    MeetingAttendee,
    Person,
    Workstream,
    WorkstreamItem,
    WorkstreamStakeholder,
)

logger = logging.getLogger("aegis.meeting_prep")

SONNET_MODEL = "claude-haiku-4-5-20251001"


async def _track_usage(
    session: AsyncSession,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Upsert LLM usage for meeting prep."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=SONNET_MODEL,
        task="meeting_prep",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        calls=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_llm_usage_daily",
        set_={
            "input_tokens": LLMUsage.input_tokens + input_tokens,
            "output_tokens": LLMUsage.output_tokens + output_tokens,
            "calls": LLMUsage.calls + 1,
        },
    )
    await session.execute(stmt)


MEETING_PREP_SYSTEM = """\
You are Aegis, an AI Chief of Staff. Generate a concise meeting preparation brief.
Focus on actionable context the user needs before walking into this meeting.
Use clear headers and bullet points. Do not use emoji."""

MEETING_PREP_USER = """\
Generate a meeting preparation brief. Structure it with:
1. Meeting Overview (title, time, attendees)
2. Attendee Profiles (recent context, role, recent interactions)
3. Open Items Involving Attendees (action items, asks pending with/from these people)
4. Linked Workstream Status (relevant workstream health)
5. Previous Meeting Context (if this is a recurring series, what happened last time)
6. Suggested Talking Points (based on open items and context)

Data:
{data}"""


async def generate_meeting_prep(session: AsyncSession, meeting_id: int) -> str:
    """Generate a meeting prep brief for a specific meeting.

    Queries attendee profiles, recent interactions, open items, linked workstreams,
    and previous meeting in recurring series. Sends to Sonnet for talking points.
    Stores in briefings table with briefing_type='meeting_prep'.

    Returns the generated briefing content.
    """
    settings = get_settings()

    # Fetch the meeting
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} not found")

    # Check if prep already exists for this meeting (avoid regenerating)
    existing_stmt = select(Briefing).where(
        Briefing.briefing_type == "meeting_prep",
        Briefing.related_meeting_id == meeting_id,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        logger.debug("Meeting prep already exists for meeting %d", meeting_id)
        return existing.content

    # Get attendees
    att_stmt = (
        select(Person)
        .join(MeetingAttendee, MeetingAttendee.person_id == Person.id)
        .where(MeetingAttendee.meeting_id == meeting_id)
    )
    att_result = await session.execute(att_stmt)
    attendees = list(att_result.scalars().all())

    attendee_ids = [a.id for a in attendees]
    attendee_profiles = []
    for person in attendees:
        profile = {
            "name": person.name,
            "title": person.title,
            "role": person.role,
            "email": person.email,
            "department_id": person.department_id,
            "is_external": person.is_external,
        }
        attendee_profiles.append(profile)

    # Recent interactions with attendees (last 30 days)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    # Recent meetings with these attendees
    if attendee_ids:
        recent_meetings_stmt = (
            select(Meeting.title, Meeting.start_time, Meeting.summary)
            .join(MeetingAttendee, MeetingAttendee.meeting_id == Meeting.id)
            .where(
                MeetingAttendee.person_id.in_(attendee_ids),
                Meeting.start_time >= thirty_days_ago,
                Meeting.id != meeting_id,
                Meeting.is_excluded.is_(False),
            )
            .distinct()
            .order_by(Meeting.start_time.desc())
            .limit(10)
        )
        recent_meetings_result = await session.execute(recent_meetings_stmt)
        recent_meetings = [
            {"title": row.title, "date": row.start_time.isoformat(), "summary": row.summary}
            for row in recent_meetings_result.all()
        ]

        # Recent emails involving attendees
        recent_emails_stmt = (
            select(Email.subject, Email.summary, Email.datetime_)
            .where(
                Email.sender_id.in_(attendee_ids),
                Email.datetime_ >= thirty_days_ago,
                Email.triage_class == "substantive",
            )
            .order_by(Email.datetime_.desc())
            .limit(10)
        )
        recent_emails_result = await session.execute(recent_emails_stmt)
        recent_emails = [
            {"subject": row.subject, "summary": row.summary, "date": row.datetime_.isoformat()}
            for row in recent_emails_result.all()
        ]
    else:
        recent_meetings = []
        recent_emails = []

    # Open items involving attendees
    open_items = []
    if attendee_ids:
        # Action items assigned to attendees
        ai_stmt = (
            select(ActionItem, Person.name)
            .outerjoin(Person, ActionItem.assignee_id == Person.id)
            .where(
                ActionItem.assignee_id.in_(attendee_ids),
                ActionItem.status.in_(["open", "in_progress"]),
            )
            .limit(15)
        )
        ai_result = await session.execute(ai_stmt)
        for row in ai_result.all():
            ai, name = row
            open_items.append({
                "type": "action_item",
                "description": ai.description,
                "assignee": name,
                "deadline": ai.deadline,
                "status": ai.status,
            })

        # Email asks targeting attendees
        ea_stmt = (
            select(EmailAsk, Person.name)
            .outerjoin(Person, EmailAsk.target_id == Person.id)
            .where(
                EmailAsk.target_id.in_(attendee_ids),
                EmailAsk.status.in_(["open", "in_progress"]),
            )
            .limit(10)
        )
        ea_result = await session.execute(ea_stmt)
        for row in ea_result.all():
            ea, name = row
            open_items.append({
                "type": "email_ask",
                "description": ea.description,
                "target": name,
                "urgency": ea.urgency,
                "ask_type": ea.ask_type,
            })

        # Chat asks targeting attendees
        ca_stmt = (
            select(ChatAsk, Person.name)
            .outerjoin(Person, ChatAsk.target_id == Person.id)
            .where(
                ChatAsk.target_id.in_(attendee_ids),
                ChatAsk.status.in_(["open", "in_progress"]),
            )
            .limit(10)
        )
        ca_result = await session.execute(ca_stmt)
        for row in ca_result.all():
            ca, name = row
            open_items.append({
                "type": "chat_ask",
                "description": ca.description,
                "target": name,
                "urgency": ca.urgency,
            })

    # Linked workstream status
    linked_workstreams = []
    if attendee_ids:
        ws_stmt = (
            select(Workstream)
            .join(WorkstreamStakeholder, WorkstreamStakeholder.workstream_id == Workstream.id)
            .where(
                WorkstreamStakeholder.person_id.in_(attendee_ids),
                Workstream.status == "active",
            )
            .distinct()
            .limit(5)
        )
        ws_result = await session.execute(ws_stmt)
        for ws in ws_result.scalars().all():
            linked_workstreams.append({
                "name": ws.name,
                "status": ws.status,
                "description": ws.description,
            })

    # Previous meeting in recurring series
    previous_meeting_data = None
    if meeting.recurring_series_id:
        prev_stmt = (
            select(Meeting)
            .where(
                Meeting.recurring_series_id == meeting.recurring_series_id,
                Meeting.start_time < meeting.start_time,
                Meeting.is_excluded.is_(False),
            )
            .order_by(Meeting.start_time.desc())
            .limit(1)
        )
        prev_result = await session.execute(prev_stmt)
        prev = prev_result.scalar_one_or_none()
        if prev:
            previous_meeting_data = {
                "title": prev.title,
                "date": prev.start_time.isoformat(),
                "summary": prev.summary,
                "sentiment": prev.sentiment,
            }

    context = {
        "meeting": {
            "title": meeting.title,
            "start_time": meeting.start_time.isoformat(),
            "end_time": meeting.end_time.isoformat(),
            "meeting_type": meeting.meeting_type,
        },
        "attendee_profiles": attendee_profiles,
        "recent_interactions": {
            "meetings": recent_meetings,
            "emails": recent_emails,
        },
        "open_items_involving_attendees": open_items,
        "linked_workstreams": linked_workstreams,
        "previous_meeting_in_series": previous_meeting_data,
    }

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.create(
        model=SONNET_MODEL,
        max_tokens=4096,
        temperature=0.3,
        system=MEETING_PREP_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": MEETING_PREP_USER.format(
                    data=json.dumps(context, default=str)
                ),
            }
        ],
    )

    content = response.content[0].text

    await _track_usage(
        session,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    # Store in briefings table
    briefing = Briefing(
        briefing_type="meeting_prep",
        related_meeting_id=meeting_id,
        content=content,
    )
    session.add(briefing)
    await session.commit()

    logger.info(
        "Generated meeting prep for '%s' (id=%d) with %d attendees, %d open items",
        meeting.title,
        meeting_id,
        len(attendees),
        len(open_items),
    )

    return content
