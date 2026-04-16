"""Response workflow routes — draft management, generation, and sending."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import (
    ActionItem,
    ChatAsk,
    ChatMessage,
    Draft,
    Email,
    EmailAsk,
    Person,
)
from aegis.intelligence.voice_profile import generate_in_voice
from aegis.web import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/respond")
settings = get_settings()


def _local_now() -> str:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.aegis_timezone)
    return datetime.now(tz).strftime("%-I:%M %p %Z")


async def _get_source_context(
    session: AsyncSession,
    source_type: str,
    source_id: int,
) -> tuple[str, str | None, int | None]:
    """Build context string from a source item. Returns (context, subject_hint, recipient_id)."""
    if source_type == "email_ask":
        ask = await session.get(EmailAsk, source_id)
        if not ask:
            return "Unknown email ask.", None, None
        email = await session.get(Email, ask.email_id) if ask.email_id else None
        target = await session.get(Person, ask.target_id) if ask.target_id else None
        requester = await session.get(Person, ask.requester_id) if ask.requester_id else None
        subject = email.subject if email else None
        context = (
            f"Email ask: {ask.description}\n"
            f"Type: {ask.ask_type}\n"
            f"From: {requester.name if requester else 'unknown'}\n"
            f"To: {target.name if target else 'unknown'}\n"
            f"Urgency: {ask.urgency}\n"
            f"Original email subject: {subject or 'unknown'}\n"
            f"Email body preview: {email.body_preview or 'N/A'}"[:500]
        )
        return context, subject, ask.target_id

    elif source_type == "chat_ask":
        ask = await session.get(ChatAsk, source_id)
        if not ask:
            return "Unknown chat ask.", None, None
        target = await session.get(Person, ask.target_id) if ask.target_id else None
        requester = await session.get(Person, ask.requester_id) if ask.requester_id else None
        msg = await session.get(ChatMessage, ask.message_id) if ask.message_id else None
        context = (
            f"Teams chat ask: {ask.description}\n"
            f"Type: {ask.ask_type}\n"
            f"From: {requester.name if requester else 'unknown'}\n"
            f"To: {target.name if target else 'unknown'}\n"
            f"Urgency: {ask.urgency}\n"
            f"Original message: {msg.body_preview or 'N/A'}"[:500] if msg else ""
        )
        return context, None, ask.target_id

    elif source_type == "action_item":
        item = await session.get(ActionItem, source_id)
        if not item:
            return "Unknown action item.", None, None
        assignee = await session.get(Person, item.assignee_id) if item.assignee_id else None
        context = (
            f"Action item: {item.description}\n"
            f"Assigned to: {assignee.name if assignee else 'unknown'}\n"
            f"Status: {item.status}\n"
            f"Deadline: {item.deadline or 'none set'}"
        )
        return context, None, item.assignee_id

    return "No source context available.", None, None


@router.get("")
async def respond_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Response workflow page — list pending drafts."""
    # Fetch pending drafts
    stmt = (
        select(Draft)
        .where(Draft.status.in_(["pending_review", "edited"]))
        .order_by(Draft.created.desc())
    )
    result = await session.execute(stmt)
    drafts = list(result.scalars().all())

    # Enrich drafts with recipient names
    recipient_ids = [d.recipient_id for d in drafts if d.recipient_id]
    recipients: dict[int, Person] = {}
    if recipient_ids:
        r_stmt = select(Person).where(Person.id.in_(recipient_ids))
        r_result = await session.execute(r_stmt)
        recipients = {p.id: p for p in r_result.scalars().all()}

    # Build draft data for template
    draft_data = []
    for d in drafts:
        recipient = recipients.get(d.recipient_id) if d.recipient_id else None
        draft_data.append({
            "id": d.id,
            "draft_type": d.draft_type,
            "channel": d.channel,
            "subject": d.subject,
            "body": d.body,
            "body_preview": (d.body or "")[:150],
            "status": d.status,
            "recipient_name": recipient.name if recipient else "Unknown",
            "recipient_email": recipient.email if recipient else None,
            "created": d.created,
            "triggered_by_type": d.triggered_by_type,
            "triggered_by_id": d.triggered_by_id,
        })

    # Count sent/discarded for stats
    sent_count_stmt = select(func.count()).select_from(Draft).where(Draft.status == "sent")
    sent_count = (await session.execute(sent_count_stmt)).scalar() or 0

    return templates.TemplateResponse(
        request,
        "respond.html",
        {
            "current_time": _local_now(),
            "page_title": "Respond",
            "drafts": draft_data,
            "sent_count": sent_count,
        },
    )


@router.post("/generate")
async def generate_draft(
    request: Request,
    directive: str = Form(...),
    source_type: str = Form(...),
    source_id: int = Form(...),
    channel: str = Form("email"),
    session: AsyncSession = Depends(get_session),
):
    """Generate a new draft from a user directive + source item context."""
    context, subject_hint, recipient_id = await _get_source_context(
        session, source_type, source_id
    )

    body = await generate_in_voice(session, directive, context, channel)

    subject = None
    if channel == "email" and subject_hint:
        subject = f"Re: {subject_hint}"
    elif channel == "email":
        subject = "Follow-up"

    draft = Draft(
        draft_type="response",
        triggered_by_type=source_type if source_type in (
            "action_item", "email_ask", "chat_ask"
        ) else None,
        triggered_by_id=source_id,
        recipient_id=recipient_id,
        channel=channel,
        subject=subject,
        body=body,
        status="pending_review",
    )
    session.add(draft)
    await session.commit()

    logger.info(
        "Generated response draft %d for %s/%d",
        draft.id, source_type, source_id,
    )

    # Return the full page (HTMX will replace the content)
    return await respond_page(request, session)


@router.post("/{draft_id}/send")
async def send_draft(
    request: Request,
    draft_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Send a draft via the appropriate channel (email or Teams)."""
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.status not in ("pending_review", "edited"):
        raise HTTPException(status_code=400, detail="Draft is not sendable")

    # Get recipient email for sending
    recipient = None
    if draft.recipient_id:
        recipient = await session.get(Person, draft.recipient_id)

    if draft.channel == "email":
        if not recipient or not recipient.email:
            raise HTTPException(
                status_code=400,
                detail="Cannot send email — recipient has no email address",
            )

        from aegis.ingestion.graph_client import GraphClient

        graph = GraphClient()
        try:
            # If we have a conversation_id, try to find the original email to reply to
            reply_to_id = None
            if draft.conversation_id:
                email_stmt = select(Email).where(
                    Email.thread_id == draft.conversation_id
                ).order_by(Email.datetime_.desc()).limit(1)
                email_result = await session.execute(email_stmt)
                source_email = email_result.scalar_one_or_none()
                if source_email:
                    reply_to_id = source_email.graph_id

            await graph.send_mail(
                subject=draft.subject or "Follow-up",
                body=draft.body,
                to=[recipient.email],
                reply_to_id=reply_to_id,
            )
        finally:
            await graph.close()

    elif draft.channel == "teams_chat":
        # Teams ChatMessage.Send — placeholder until fully wired
        logger.warning(
            "Teams send not yet fully wired — draft %d marked as sent", draft_id
        )

    # Update draft status
    draft.status = "sent"
    draft.sent_at = datetime.now(timezone.utc)
    await session.commit()

    # Update the source item status if applicable
    await _mark_source_completed(session, draft)

    logger.info("Sent draft %d via %s", draft_id, draft.channel)

    return await respond_page(request, session)


@router.post("/{draft_id}/discard")
async def discard_draft(
    request: Request,
    draft_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Discard a draft."""
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    draft.status = "discarded"
    await session.commit()

    logger.info("Discarded draft %d", draft_id)
    return await respond_page(request, session)


@router.post("/{draft_id}/edit")
async def edit_draft(
    request: Request,
    draft_id: int,
    body: str = Form(...),
    subject: str = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Update a draft's body (and optionally subject)."""
    draft = await session.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    draft.body = body
    if subject is not None:
        draft.subject = subject
    draft.status = "edited"
    await session.commit()

    logger.info("Edited draft %d", draft_id)
    return await respond_page(request, session)


async def _mark_source_completed(session: AsyncSession, draft: Draft) -> None:
    """After sending, mark the source ask/action_item as completed."""
    if not draft.triggered_by_type or not draft.triggered_by_id:
        return

    try:
        if draft.triggered_by_type == "email_ask":
            stmt = (
                update(EmailAsk)
                .where(EmailAsk.id == draft.triggered_by_id)
                .values(status="completed", updated=datetime.now(timezone.utc))
            )
            await session.execute(stmt)
        elif draft.triggered_by_type == "chat_ask":
            stmt = (
                update(ChatAsk)
                .where(ChatAsk.id == draft.triggered_by_id)
                .values(status="completed", updated=datetime.now(timezone.utc))
            )
            await session.execute(stmt)
        elif draft.triggered_by_type == "action_item":
            stmt = (
                update(ActionItem)
                .where(ActionItem.id == draft.triggered_by_id)
                .values(status="completed", updated=datetime.now(timezone.utc))
            )
            await session.execute(stmt)
        await session.commit()
    except Exception:
        logger.exception(
            "Failed to mark source %s/%d as completed",
            draft.triggered_by_type, draft.triggered_by_id,
        )
