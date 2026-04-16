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
from aegis.web import templates

router = APIRouter(prefix="/asks")
settings = get_settings()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


@router.get("")
async def asks_list(
    request: Request,
    tab: str = Query("all", description="Tab: all or awaiting"),
    status: str = Query("", description="Filter by status"),
    urgency: str = Query("", description="Filter by urgency"),
    ask_type: str = Query("", description="Filter by ask type"),
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_session),
):
    per_page = 25
    tz = _local_tz()

    # For "awaiting" tab, force status to open
    effective_status = status or None
    if tab == "awaiting":
        effective_status = "open"

    asks, total = await get_all_asks(
        session,
        status=effective_status,
        urgency=urgency or None,
        ask_type=ask_type or None,
        page=page,
        per_page=per_page,
    )

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
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
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

    # Return updated status badge via HTMX
    badge_colors = {
        "open": "bg-yellow-50 text-yellow-700",
        "in_progress": "bg-blue-50 text-blue-700",
        "completed": "bg-green-50 text-green-700",
        "stale": "bg-red-50 text-red-600",
    }
    color = badge_colors.get(new_status, "bg-gray-100 text-gray-600")
    label = new_status.replace("_", " ").title()

    html = (
        f'<span class="inline-flex items-center rounded-full px-2 py-1 text-xs font-medium {color}">'
        f'{label}</span>'
    )
    return HTMLResponse(html)
