"""Asks routes — combined email_asks + chat_asks with status management."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.repositories import (
    get_all_asks,
    get_persons_by_ids,
    update_chat_ask_status,
    update_email_ask_status,
)
from aegis.db.models import Person
from aegis.web import templates
from sqlalchemy import select

router = APIRouter(prefix="/asks")
settings = get_settings()

_STATUS_OPTIONS = ["open", "in_progress", "completed", "stale"]
_STATUS_COLORS = {
    "open": "bg-yellow-50 text-yellow-700",
    "in_progress": "bg-blue-50 text-blue-700",
    "completed": "bg-green-50 text-green-700",
    "stale": "bg-red-50 text-red-600",
}
_STATUS_LABELS = {
    "open": "Open",
    "in_progress": "In Progress",
    "completed": "Completed",
    "stale": "Stale",
}


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


async def _get_user_person_id(session: AsyncSession) -> int | None:
    """Look up the user's person_id from their configured email."""
    user_email = settings.user_email
    if not user_email:
        return None
    stmt = select(Person.id).where(Person.email == user_email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


@router.get("")
async def asks_list(
    request: Request,
    tab: str = Query("all", description="Tab: all, inbound, or outbound"),
    status: str = Query("", description="Filter by status"),
    urgency: str = Query("", description="Filter by urgency"),
    ask_type: str = Query("", description="Filter by ask type"),
    source: str = Query("", description="Filter by source: all, email, chat"),
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_session),
):
    per_page = 25
    tz = _local_tz()

    # Resolve user person_id for directionality filtering
    user_person_id = await _get_user_person_id(session)

    effective_status = status or None

    asks, total = await get_all_asks(
        session,
        status=effective_status,
        urgency=urgency or None,
        ask_type=ask_type or None,
        source=source or None,
        page=page,
        per_page=per_page,
    )

    # Apply directionality filter based on tab
    if tab == "inbound" and user_person_id:
        asks = [a for a in asks if a.get("target_id") == user_person_id]
        total = len(asks)
    elif tab == "outbound" and user_person_id:
        asks = [a for a in asks if a.get("requester_id") == user_person_id]
        total = len(asks)

    # Collect person IDs for name lookup
    person_ids = set()
    for ask in asks:
        if ask.get("requester_id"):
            person_ids.add(ask["requester_id"])
        if ask.get("target_id"):
            person_ids.add(ask["target_id"])
    person_map = await get_persons_by_ids(session, list(person_ids)) if person_ids else {}

    total_pages = max(1, (total + per_page - 1) // per_page)
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "asks.html",
        {
            "asks": asks,
            "person_map": person_map,
            "tab": tab,
            "status": status,
            "urgency": urgency,
            "ask_type": ask_type,
            "source": source,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
            "status_options": _STATUS_OPTIONS,
            "status_colors": _STATUS_COLORS,
            "status_labels": _STATUS_LABELS,
        },
    )


@router.post("/{source_type}/{ask_id}/status")
async def update_ask_status(
    request: Request,
    source_type: str,
    ask_id: int,
    new_status: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Update ask status via HTMX. source_type is 'email' or 'chat'."""
    valid_statuses = {"open", "in_progress", "completed", "stale"}
    if new_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    if source_type == "email":
        await update_email_ask_status(session, ask_id, new_status)
    elif source_type == "chat":
        await update_chat_ask_status(session, ask_id, new_status)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid source type: {source_type}")

    # Return click-to-cycle badge (matching actions page pattern)
    current_idx = _STATUS_OPTIONS.index(new_status)
    next_status = _STATUS_OPTIONS[(current_idx + 1) % len(_STATUS_OPTIONS)]

    color = _STATUS_COLORS.get(new_status, "bg-gray-100 text-gray-600")
    label = _STATUS_LABELS.get(new_status, new_status)

    html = (
        f'<form hx-post="/asks/{source_type}/{ask_id}/status" hx-swap="outerHTML" hx-target="this">'
        f'<input type="hidden" name="new_status" value="{next_status}">'
        f'<button type="submit" '
        f'class="inline-flex items-center rounded-full px-2 py-1 text-xs font-medium cursor-pointer '
        f'transition-colors hover:opacity-80 {color}">'
        f'{label}</button></form>'
    )
    return HTMLResponse(html)
