"""Emails routes — email browser with filtering, detail view."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.repositories import (
    get_email_asks_for_email,
    get_email_by_id,
    get_emails,
    get_persons_by_ids,
)
from aegis.web import templates

router = APIRouter(prefix="/emails")
settings = get_settings()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


@router.get("")
async def emails_list(
    request: Request,
    q: str = Query("", description="Search by subject"),
    email_class: str = Query("", description="Filter by email class"),
    intent: str = Query("", description="Filter by intent"),
    triage_class: str = Query("", description="Filter by triage class"),
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_session),
):
    per_page = 25
    tz = _local_tz()

    emails, total = await get_emails(
        session,
        email_class=email_class or None,
        intent=intent or None,
        triage_class=triage_class or None,
        search=q or None,
        page=page,
        per_page=per_page,
    )

    # Collect sender IDs for name lookup
    sender_ids = [e.sender_id for e in emails if e.sender_id]
    sender_map = await get_persons_by_ids(session, sender_ids) if sender_ids else {}

    total_pages = max(1, (total + per_page - 1) // per_page)
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "emails.html",
        {
            "emails": emails,
            "sender_map": sender_map,
            "q": q,
            "email_class": email_class,
            "intent": intent,
            "triage_class": triage_class,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )


@router.get("/{email_id}")
async def email_detail(
    request: Request,
    email_id: int,
    session: AsyncSession = Depends(get_session),
):
    email = await get_email_by_id(session, email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    asks = await get_email_asks_for_email(session, email_id)
    tz = _local_tz()
    now_local = datetime.now(tz)

    # Get sender name
    sender_name = None
    if email.sender_id:
        from aegis.db.repositories import get_person_by_id

        sender = await get_person_by_id(session, email.sender_id)
        sender_name = sender.name if sender else None

    # Get person names for asks
    person_ids = set()
    for ask in asks:
        if ask.requester_id:
            person_ids.add(ask.requester_id)
        if ask.target_id:
            person_ids.add(ask.target_id)
    person_map = await get_persons_by_ids(session, list(person_ids)) if person_ids else {}

    return templates.TemplateResponse(
        request,
        "email_detail.html",
        {
            "email": email,
            "asks": asks,
            "sender_name": sender_name,
            "person_map": person_map,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )
