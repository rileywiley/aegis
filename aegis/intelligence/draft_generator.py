"""Draft generator — auto-creates nudges for stale items and recaps for meetings."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import (
    ActionItem,
    ChatAsk,
    Draft,
    Email,
    EmailAsk,
    Meeting,
    Person,
)
from aegis.intelligence.voice_profile import generate_in_voice

logger = logging.getLogger(__name__)


async def generate_stale_nudges(session: AsyncSession) -> int:
    """Generate nudge drafts for stale action items and asks.

    Finds items past the stale_nudge_threshold_days that do not already
    have a pending nudge draft. Creates a draft for each using the user's voice.

    Returns:
        Number of nudge drafts generated.
    """
    settings = get_settings()
    threshold = datetime.now(timezone.utc) - timedelta(days=settings.stale_nudge_threshold_days)
    count = 0

    # --- Stale action items ---
    stmt = (
        select(ActionItem)
        .where(
            ActionItem.status.in_(["open", "in_progress"]),
            ActionItem.created < threshold,
        )
        .order_by(ActionItem.created)
        .limit(20)
    )
    result = await session.execute(stmt)
    stale_actions = list(result.scalars().all())

    for item in stale_actions:
        # Skip if a pending nudge draft already exists for this item
        existing_stmt = select(Draft).where(
            Draft.triggered_by_type == "action_item",
            Draft.triggered_by_id == item.id,
            Draft.draft_type == "nudge",
            Draft.status == "pending_review",
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            continue

        # Get assignee info
        assignee = None
        if item.assignee_id:
            assignee = await session.get(Person, item.assignee_id)

        assignee_name = assignee.name if assignee else "the assignee"
        assignee_email = assignee.email if assignee else None

        context = (
            f"Action item: {item.description}\n"
            f"Assigned to: {assignee_name}\n"
            f"Created: {item.created.strftime('%Y-%m-%d') if item.created else 'unknown'}\n"
            f"Deadline: {item.deadline or 'none set'}\n"
            f"Status: {item.status}"
        )
        directive = (
            f"Send a friendly nudge to {assignee_name} about this overdue action item. "
            f"Ask for a status update. Keep it brief and non-confrontational."
        )

        try:
            body = await generate_in_voice(session, directive, context, "email")

            draft = Draft(
                draft_type="nudge",
                triggered_by_type="action_item",
                triggered_by_id=item.id,
                recipient_id=item.assignee_id,
                channel="email",
                subject=f"Quick check-in: {item.description[:80]}",
                body=body,
                status="pending_review",
            )
            session.add(draft)
            await session.flush()
            count += 1
        except Exception:
            logger.exception(
                "Failed to generate nudge for action_item %d", item.id
            )

    # --- Stale email asks ---
    ask_threshold = datetime.now(timezone.utc) - timedelta(hours=settings.stale_ask_hours)
    stmt = (
        select(EmailAsk)
        .where(
            EmailAsk.status.in_(["open", "in_progress"]),
            EmailAsk.created < ask_threshold,
        )
        .order_by(EmailAsk.created)
        .limit(20)
    )
    result = await session.execute(stmt)
    stale_email_asks = list(result.scalars().all())

    for ask in stale_email_asks:
        existing_stmt = select(Draft).where(
            Draft.triggered_by_type == "email_ask",
            Draft.triggered_by_id == ask.id,
            Draft.draft_type == "nudge",
            Draft.status == "pending_review",
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            continue

        target = None
        if ask.target_id:
            target = await session.get(Person, ask.target_id)

        # Get the source email for threading context
        source_email = await session.get(Email, ask.email_id) if ask.email_id else None

        target_name = target.name if target else "the recipient"

        context = (
            f"Email ask: {ask.description}\n"
            f"Type: {ask.ask_type}\n"
            f"Directed to: {target_name}\n"
            f"Urgency: {ask.urgency}\n"
            f"Created: {ask.created.strftime('%Y-%m-%d') if ask.created else 'unknown'}\n"
            f"Original subject: {source_email.subject if source_email else 'unknown'}"
        )
        directive = (
            f"Send a follow-up to {target_name} about this pending ask. "
            f"Reference the original request and ask for an update."
        )

        try:
            body = await generate_in_voice(session, directive, context, "email")

            draft = Draft(
                draft_type="nudge",
                triggered_by_type="email_ask",
                triggered_by_id=ask.id,
                recipient_id=ask.target_id,
                channel="email",
                subject=f"Following up: {source_email.subject if source_email else ask.description[:80]}",
                body=body,
                conversation_id=source_email.thread_id if source_email else None,
                status="pending_review",
            )
            session.add(draft)
            await session.flush()
            count += 1
        except Exception:
            logger.exception(
                "Failed to generate nudge for email_ask %d", ask.id
            )

    # --- Stale chat asks ---
    stmt = (
        select(ChatAsk)
        .where(
            ChatAsk.status.in_(["open", "in_progress"]),
            ChatAsk.created < ask_threshold,
        )
        .order_by(ChatAsk.created)
        .limit(20)
    )
    result = await session.execute(stmt)
    stale_chat_asks = list(result.scalars().all())

    for ask in stale_chat_asks:
        existing_stmt = select(Draft).where(
            Draft.triggered_by_type == "chat_ask",
            Draft.triggered_by_id == ask.id,
            Draft.draft_type == "nudge",
            Draft.status == "pending_review",
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            continue

        target = None
        if ask.target_id:
            target = await session.get(Person, ask.target_id)

        target_name = target.name if target else "the recipient"

        context = (
            f"Teams chat ask: {ask.description}\n"
            f"Type: {ask.ask_type}\n"
            f"Directed to: {target_name}\n"
            f"Urgency: {ask.urgency}\n"
            f"Created: {ask.created.strftime('%Y-%m-%d') if ask.created else 'unknown'}"
        )
        directive = (
            f"Send a follow-up to {target_name} about this pending Teams chat ask. "
            f"Keep it brief and conversational."
        )

        try:
            body = await generate_in_voice(session, directive, context, "teams_chat")

            draft = Draft(
                draft_type="nudge",
                triggered_by_type="chat_ask",
                triggered_by_id=ask.id,
                recipient_id=ask.target_id,
                channel="teams_chat",
                subject=None,
                body=body,
                status="pending_review",
            )
            session.add(draft)
            await session.flush()
            count += 1
        except Exception:
            logger.exception(
                "Failed to generate nudge for chat_ask %d", ask.id
            )

    await session.commit()
    if count:
        logger.info("Generated %d stale nudge drafts", count)
    return count


async def generate_meeting_recaps(session: AsyncSession) -> int:
    """Generate recap drafts for completed meetings that have transcripts.

    Finds completed meetings with transcript_status='captured' that do not
    already have a recap draft. Generates a recap summary in the user's voice.

    Returns:
        Number of recap drafts generated.
    """
    # Find completed meetings with transcripts but no recap draft
    stmt = (
        select(Meeting)
        .where(
            Meeting.status == "completed",
            Meeting.transcript_status.in_(["captured", "partial"]),
            Meeting.is_excluded.is_(False),
        )
        .order_by(Meeting.end_time.desc())
        .limit(10)
    )
    result = await session.execute(stmt)
    meetings = list(result.scalars().all())

    count = 0
    for meeting in meetings:
        # Skip if recap draft already exists
        existing_stmt = select(Draft).where(
            Draft.triggered_by_type == "meeting",
            Draft.triggered_by_id == meeting.id,
            Draft.draft_type == "recap",
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            continue

        # Build context from meeting data
        summary = meeting.summary or "No summary available"
        transcript_preview = (meeting.transcript_text or "")[:2000]

        context = (
            f"Meeting: {meeting.title}\n"
            f"Date: {meeting.start_time.strftime('%Y-%m-%d %H:%M') if meeting.start_time else 'unknown'}\n"
            f"Duration: {meeting.duration or 'unknown'} minutes\n"
            f"Summary: {summary}\n"
            f"Transcript excerpt:\n{transcript_preview}"
        )
        directive = (
            f"Write a concise meeting recap email for '{meeting.title}'. "
            f"Include key decisions, action items, and next steps. "
            f"Format with bullet points for easy scanning. "
            f"Keep it professional but in my voice."
        )

        try:
            body = await generate_in_voice(session, directive, context, "email")

            draft = Draft(
                draft_type="recap",
                triggered_by_type="meeting",
                triggered_by_id=meeting.id,
                channel="email",
                subject=f"Recap: {meeting.title}",
                body=body,
                status="pending_review",
            )
            session.add(draft)
            await session.flush()
            count += 1
        except Exception:
            logger.exception(
                "Failed to generate recap for meeting %d", meeting.id
            )

    await session.commit()
    if count:
        logger.info("Generated %d meeting recap drafts", count)
    return count
