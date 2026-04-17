"""Briefing generators — morning, Monday, and Friday briefings via Sonnet.

Generates structured briefings from meetings, action items, asks, workstreams,
and overnight activity. Stores results in the briefings table and tracks LLM usage.
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
    Commitment,
    Decision,
    Draft,
    Email,
    EmailAsk,
    LLMUsage,
    Meeting,
    MeetingAttendee,
    Person,
    Workstream,
)
from aegis.intelligence.meeting_prep import generate_meeting_prep

logger = logging.getLogger("aegis.briefings")

SONNET_MODEL = "claude-haiku-4-5-20251001"


# ── LLM usage tracking ──────────────────────────────────────


async def _track_usage(
    session: AsyncSession,
    task: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Upsert LLM usage for the given task."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=SONNET_MODEL,
        task=task,
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


# ── Helpers ──────────────────────────────────────────────────


def _local_now() -> datetime:
    """Get current time in the configured timezone."""
    import zoneinfo

    settings = get_settings()
    tz = zoneinfo.ZoneInfo(settings.aegis_timezone)
    return datetime.now(tz)


def _today_range_utc() -> tuple[datetime, datetime]:
    """Return (start_of_today, end_of_today) in UTC based on local timezone."""
    import zoneinfo

    settings = get_settings()
    tz = zoneinfo.ZoneInfo(settings.aegis_timezone)
    local_now = datetime.now(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _week_range_utc() -> tuple[datetime, datetime]:
    """Return (start_of_this_week_monday, end_of_friday) in UTC."""
    import zoneinfo

    settings = get_settings()
    tz = zoneinfo.ZoneInfo(settings.aegis_timezone)
    local_now = datetime.now(tz)
    # Monday of this week
    monday = local_now - timedelta(days=local_now.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    return week_start.astimezone(timezone.utc), week_end.astimezone(timezone.utc)


async def _get_todays_meetings(session: AsyncSession) -> list[dict]:
    """Fetch today's meetings with attendee names."""
    day_start, day_end = _today_range_utc()
    stmt = (
        select(Meeting)
        .where(
            Meeting.start_time >= day_start,
            Meeting.start_time < day_end,
            Meeting.is_excluded.is_(False),
        )
        .order_by(Meeting.start_time)
    )
    result = await session.execute(stmt)
    meetings = list(result.scalars().all())

    meeting_data = []
    for m in meetings:
        att_stmt = (
            select(Person.name)
            .join(MeetingAttendee, MeetingAttendee.person_id == Person.id)
            .where(MeetingAttendee.meeting_id == m.id)
        )
        att_result = await session.execute(att_stmt)
        attendees = [row for row in att_result.scalars().all()]
        meeting_data.append({
            "id": m.id,
            "title": m.title,
            "start_time": m.start_time.isoformat(),
            "end_time": m.end_time.isoformat(),
            "attendees": attendees,
            "status": m.status,
            "summary": m.summary,
            "recurring_series_id": m.recurring_series_id,
        })
    return meeting_data


async def _get_requires_action(session: AsyncSession) -> dict:
    """Get items requiring user action: open action items, high-urgency asks, stale items."""
    # Open action items
    ai_stmt = (
        select(ActionItem)
        .where(ActionItem.status.in_(["open", "in_progress"]))
        .order_by(ActionItem.created.desc())
        .limit(20)
    )
    ai_result = await session.execute(ai_stmt)
    action_items = [
        {"description": a.description, "status": a.status, "deadline": a.deadline}
        for a in ai_result.scalars().all()
    ]

    # High urgency email asks
    ea_stmt = (
        select(EmailAsk)
        .where(EmailAsk.status == "open", EmailAsk.urgency == "high")
        .order_by(EmailAsk.created.desc())
        .limit(10)
    )
    ea_result = await session.execute(ea_stmt)
    email_asks = [
        {"description": a.description, "ask_type": a.ask_type, "urgency": a.urgency}
        for a in ea_result.scalars().all()
    ]

    # Stale items
    stale_stmt = (
        select(ActionItem)
        .where(ActionItem.status == "stale")
        .order_by(ActionItem.created.desc())
        .limit(10)
    )
    stale_result = await session.execute(stale_stmt)
    stale_items = [
        {"description": a.description, "deadline": a.deadline}
        for a in stale_result.scalars().all()
    ]

    return {
        "action_items": action_items,
        "high_urgency_asks": email_asks,
        "stale_items": stale_items,
    }


async def _get_overnight_activity(session: AsyncSession) -> dict:
    """Get emails and chat messages received since yesterday 6pm local time."""
    import zoneinfo

    settings = get_settings()
    tz = zoneinfo.ZoneInfo(settings.aegis_timezone)
    local_now = datetime.now(tz)
    yesterday_6pm = (local_now - timedelta(days=1)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    cutoff = yesterday_6pm.astimezone(timezone.utc)

    email_stmt = (
        select(func.count())
        .select_from(Email)
        .where(Email.datetime_ >= cutoff, Email.email_class == "human")
    )
    email_count = (await session.execute(email_stmt)).scalar_one() or 0

    chat_stmt = (
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.datetime_ >= cutoff, ChatMessage.noise_filtered.is_(False))
    )
    chat_count = (await session.execute(chat_stmt)).scalar_one() or 0

    # Get summaries of substantive overnight emails
    sub_stmt = (
        select(Email.subject, Email.summary)
        .where(
            Email.datetime_ >= cutoff,
            Email.triage_class == "substantive",
        )
        .order_by(Email.datetime_.desc())
        .limit(10)
    )
    sub_result = await session.execute(sub_stmt)
    substantive_emails = [
        {"subject": row.subject, "summary": row.summary}
        for row in sub_result.all()
    ]

    return {
        "email_count": email_count,
        "chat_count": chat_count,
        "substantive_emails": substantive_emails,
    }


async def _get_workstream_health(session: AsyncSession) -> list[dict]:
    """Get active workstream health overview."""
    stmt = (
        select(Workstream)
        .where(Workstream.status.in_(["active", "quiet"]))
        .order_by(Workstream.pinned.desc(), Workstream.updated.desc())
        .limit(10)
    )
    result = await session.execute(stmt)
    workstreams = list(result.scalars().all())

    ws_data = []
    for ws in workstreams:
        # Count open items in this workstream
        from aegis.db.models import WorkstreamItem

        item_count_stmt = (
            select(func.count())
            .select_from(WorkstreamItem)
            .where(WorkstreamItem.workstream_id == ws.id)
        )
        item_count = (await session.execute(item_count_stmt)).scalar_one() or 0

        ws_data.append({
            "name": ws.name,
            "status": ws.status,
            "description": ws.description,
            "item_count": item_count,
            "updated": ws.updated.isoformat() if ws.updated else None,
        })
    return ws_data


async def _get_pending_drafts_count(session: AsyncSession) -> int:
    """Count pending review drafts."""
    stmt = (
        select(func.count())
        .select_from(Draft)
        .where(Draft.status == "pending_review")
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


async def _call_sonnet(
    session: AsyncSession,
    system_prompt: str,
    user_prompt: str,
    task: str,
) -> str:
    """Call Sonnet with the given prompts, track usage, return text response."""
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.create(
        model=SONNET_MODEL,
        max_tokens=4096,
        temperature=0.3,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    content = response.content[0].text

    await _track_usage(
        session,
        task=task,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    return content


# ── Morning Briefing ─────────────────────────────────────────


MORNING_SYSTEM = """\
You are Aegis, an AI Chief of Staff. Generate a concise, actionable morning briefing.
Use clear headers and bullet points. Be direct — the user is busy.
Focus on what requires attention today. Do not use emoji."""

MORNING_USER = """\
Generate a morning briefing using this data. Structure it with these sections:
1. Today's Meetings (with suggested topics per meeting based on open items with attendees)
2. Requires Your Action (decisions needed, pending asks, stale items)
3. Overnight Activity Summary
4. Workstream Health Overview
5. Drafts Ready for Review

Data:
{data}"""


async def generate_morning_briefing(session: AsyncSession) -> str:
    """Generate the daily morning briefing.

    Queries today's meetings, action items, overnight activity, workstream health,
    and pending drafts. Sends to Sonnet for formatting. Stores in briefings table.
    Also pre-generates meeting prep briefs for each of today's meetings.
    """
    meetings = await _get_todays_meetings(session)
    requires_action = await _get_requires_action(session)
    overnight = await _get_overnight_activity(session)
    workstreams = await _get_workstream_health(session)
    drafts_count = await _get_pending_drafts_count(session)

    context = {
        "todays_meetings": meetings,
        "requires_action": requires_action,
        "overnight_activity": overnight,
        "workstream_health": workstreams,
        "pending_drafts_count": drafts_count,
        "date": _local_now().strftime("%A, %B %d, %Y"),
    }

    briefing_content = await _call_sonnet(
        session,
        system_prompt=MORNING_SYSTEM,
        user_prompt=MORNING_USER.format(data=json.dumps(context, default=str)),
        task="morning_briefing",
    )

    # Store briefing
    briefing = Briefing(
        briefing_type="morning",
        content=briefing_content,
    )
    session.add(briefing)
    await session.commit()

    # Pre-generate meeting prep for each of today's meetings
    for meeting in meetings:
        try:
            await generate_meeting_prep(session, meeting["id"])
        except Exception:
            logger.exception(
                "Failed to generate meeting prep for meeting %d", meeting["id"]
            )

    logger.info("Generated morning briefing with %d meetings", len(meetings))
    return briefing_content


# ── Monday Brief ─────────────────────────────────────────────


MONDAY_SYSTEM = """\
You are Aegis, an AI Chief of Staff. Generate a Monday planning brief.
Identify the top 3-5 objectives for this week based on the data.
Be strategic — help the user prioritize. Do not use emoji."""

MONDAY_USER = """\
Generate a Monday planning brief for the week. Structure it with:
1. This Week's Objectives (LLM-identified, 3-5 priorities)
2. Calendar Overview (key meetings this week)
3. Deadlines This Week (action items and asks with upcoming deadlines)
4. Workstreams Needing Attention (stale, overdue, or at risk)
5. Carryover from Last Week (incomplete items)

Data:
{data}"""


async def generate_monday_brief(session: AsyncSession) -> str:
    """Generate the Monday planning brief (replaces morning briefing on Mondays).

    Queries this week's calendar, deadlines, workstreams needing attention,
    and carryover from last week.
    """
    week_start, week_end = _week_range_utc()

    # This week's meetings
    meetings_stmt = (
        select(Meeting)
        .where(
            Meeting.start_time >= week_start,
            Meeting.start_time < week_end,
            Meeting.is_excluded.is_(False),
        )
        .order_by(Meeting.start_time)
    )
    meetings_result = await session.execute(meetings_stmt)
    meetings = [
        {"title": m.title, "start_time": m.start_time.isoformat(), "status": m.status}
        for m in meetings_result.scalars().all()
    ]

    # Deadlines this week (action items with deadline text containing this week's dates)
    # Since deadlines are stored as text, we do a best-effort query for open items
    open_ai_stmt = (
        select(ActionItem)
        .where(ActionItem.status.in_(["open", "in_progress"]))
        .order_by(ActionItem.created.desc())
        .limit(30)
    )
    open_ai_result = await session.execute(open_ai_stmt)
    open_action_items = [
        {"description": a.description, "deadline": a.deadline, "status": a.status}
        for a in open_ai_result.scalars().all()
    ]

    # Open email asks with deadlines
    open_ea_stmt = (
        select(EmailAsk)
        .where(EmailAsk.status.in_(["open", "in_progress"]))
        .order_by(EmailAsk.created.desc())
        .limit(20)
    )
    open_ea_result = await session.execute(open_ea_stmt)
    open_email_asks = [
        {
            "description": a.description,
            "deadline": a.deadline,
            "urgency": a.urgency,
            "ask_type": a.ask_type,
        }
        for a in open_ea_result.scalars().all()
    ]

    # Workstreams needing attention
    ws_stmt = (
        select(Workstream)
        .where(Workstream.status.in_(["active", "quiet"]))
        .order_by(Workstream.updated.asc())  # oldest updated first = needs attention
        .limit(10)
    )
    ws_result = await session.execute(ws_stmt)
    workstreams = [
        {"name": ws.name, "status": ws.status, "updated": ws.updated.isoformat() if ws.updated else None}
        for ws in ws_result.scalars().all()
    ]

    # Carryover: incomplete action items from before this week
    carryover_stmt = (
        select(ActionItem)
        .where(
            ActionItem.status.in_(["open", "in_progress"]),
            ActionItem.created < week_start,
        )
        .order_by(ActionItem.created.desc())
        .limit(15)
    )
    carryover_result = await session.execute(carryover_stmt)
    carryover = [
        {"description": a.description, "deadline": a.deadline, "created": a.created.isoformat()}
        for a in carryover_result.scalars().all()
    ]

    context = {
        "week_meetings": meetings,
        "open_action_items": open_action_items,
        "open_email_asks": open_email_asks,
        "workstreams_needing_attention": workstreams,
        "carryover_from_last_week": carryover,
        "week_of": _local_now().strftime("%B %d, %Y"),
    }

    briefing_content = await _call_sonnet(
        session,
        system_prompt=MONDAY_SYSTEM,
        user_prompt=MONDAY_USER.format(data=json.dumps(context, default=str)),
        task="monday_briefing",
    )

    briefing = Briefing(
        briefing_type="monday",
        content=briefing_content,
    )
    session.add(briefing)
    await session.commit()

    # Also pre-generate today's meeting preps
    todays_meetings = await _get_todays_meetings(session)
    for meeting in todays_meetings:
        try:
            await generate_meeting_prep(session, meeting["id"])
        except Exception:
            logger.exception(
                "Failed to generate meeting prep for meeting %d", meeting["id"]
            )

    logger.info(
        "Generated Monday brief: %d meetings this week, %d carryover items",
        len(meetings),
        len(carryover),
    )
    return briefing_content


# ── Friday Recap ─────────────────────────────────────────────


FRIDAY_SYSTEM = """\
You are Aegis, an AI Chief of Staff. Generate a Friday end-of-week recap.
Summarize accomplishments and flag items needing attention next week.
Be concise and highlight patterns. Do not use emoji."""

FRIDAY_USER = """\
Generate a Friday recap for this week. Structure it with:
1. Decisions Made This Week
2. Commitment Tracker (made / completed / overdue)
3. Ask Completion Rate (completed vs total)
4. Workstream Summary (progress across active workstreams)
5. Items Carrying Into Next Week

Data:
{data}"""


async def generate_friday_recap(session: AsyncSession) -> str:
    """Generate the Friday end-of-week recap.

    Queries decisions, commitments, ask completion rates, and workstream summaries.
    """
    week_start, week_end = _week_range_utc()

    # Decisions made this week
    decisions_stmt = (
        select(Decision)
        .where(Decision.datetime_ >= week_start, Decision.datetime_ < week_end)
        .order_by(Decision.datetime_.desc())
    )
    decisions_result = await session.execute(decisions_stmt)
    decisions = [
        {"description": d.description, "date": d.datetime_.isoformat()}
        for d in decisions_result.scalars().all()
    ]

    # Commitment tracker
    # Created this week
    new_commitments_stmt = (
        select(func.count())
        .select_from(Commitment)
        .where(Commitment.created >= week_start, Commitment.created < week_end)
    )
    new_commitments = (await session.execute(new_commitments_stmt)).scalar_one() or 0

    # Completed this week (status changed to completed)
    completed_commitments_stmt = (
        select(func.count())
        .select_from(Commitment)
        .where(Commitment.status == "completed")
    )
    completed_commitments = (await session.execute(completed_commitments_stmt)).scalar_one() or 0

    # Overdue
    overdue_commitments_stmt = (
        select(func.count())
        .select_from(Commitment)
        .where(Commitment.status == "overdue")
    )
    overdue_commitments = (await session.execute(overdue_commitments_stmt)).scalar_one() or 0

    # Ask completion rate
    total_email_asks_stmt = (
        select(func.count())
        .select_from(EmailAsk)
        .where(EmailAsk.created >= week_start, EmailAsk.created < week_end)
    )
    total_email_asks = (await session.execute(total_email_asks_stmt)).scalar_one() or 0

    completed_email_asks_stmt = (
        select(func.count())
        .select_from(EmailAsk)
        .where(
            EmailAsk.created >= week_start,
            EmailAsk.created < week_end,
            EmailAsk.status == "completed",
        )
    )
    completed_email_asks = (await session.execute(completed_email_asks_stmt)).scalar_one() or 0

    total_chat_asks_stmt = (
        select(func.count())
        .select_from(ChatAsk)
        .where(ChatAsk.created >= week_start, ChatAsk.created < week_end)
    )
    total_chat_asks = (await session.execute(total_chat_asks_stmt)).scalar_one() or 0

    completed_chat_asks_stmt = (
        select(func.count())
        .select_from(ChatAsk)
        .where(
            ChatAsk.created >= week_start,
            ChatAsk.created < week_end,
            ChatAsk.status == "completed",
        )
    )
    completed_chat_asks = (await session.execute(completed_chat_asks_stmt)).scalar_one() or 0

    total_asks = total_email_asks + total_chat_asks
    completed_asks = completed_email_asks + completed_chat_asks

    # Workstream summary
    ws_stmt = (
        select(Workstream)
        .where(Workstream.status == "active")
        .order_by(Workstream.pinned.desc(), Workstream.updated.desc())
        .limit(10)
    )
    ws_result = await session.execute(ws_stmt)
    workstreams = [
        {"name": ws.name, "description": ws.description, "updated": ws.updated.isoformat() if ws.updated else None}
        for ws in ws_result.scalars().all()
    ]

    # Items carrying into next week
    carryover_stmt = (
        select(ActionItem)
        .where(ActionItem.status.in_(["open", "in_progress"]))
        .order_by(ActionItem.created.desc())
        .limit(15)
    )
    carryover_result = await session.execute(carryover_stmt)
    carryover = [
        {"description": a.description, "deadline": a.deadline}
        for a in carryover_result.scalars().all()
    ]

    context = {
        "decisions_this_week": decisions,
        "commitments": {
            "new_this_week": new_commitments,
            "completed": completed_commitments,
            "overdue": overdue_commitments,
        },
        "ask_completion": {
            "total": total_asks,
            "completed": completed_asks,
            "rate": f"{(completed_asks / total_asks * 100):.0f}%" if total_asks > 0 else "N/A",
        },
        "workstreams": workstreams,
        "carrying_into_next_week": carryover,
        "week_ending": _local_now().strftime("%B %d, %Y"),
    }

    briefing_content = await _call_sonnet(
        session,
        system_prompt=FRIDAY_SYSTEM,
        user_prompt=FRIDAY_USER.format(data=json.dumps(context, default=str)),
        task="friday_recap",
    )

    briefing = Briefing(
        briefing_type="friday",
        content=briefing_content,
    )
    session.add(briefing)
    await session.commit()

    logger.info(
        "Generated Friday recap: %d decisions, %d/%d asks completed",
        len(decisions),
        completed_asks,
        total_asks,
    )
    return briefing_content
