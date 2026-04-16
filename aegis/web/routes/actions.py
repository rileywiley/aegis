"""Action items routes — list with filters, status updates."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.repositories import (
    get_action_items,
    get_persons_by_ids,
    update_action_item_status,
)
from aegis.web import templates

router = APIRouter(prefix="/actions")
settings = get_settings()

_STATUS_OPTIONS = ["open", "in_progress", "completed", "stale"]

_STATUS_COLORS = {
    "open": "bg-blue-50 text-blue-700",
    "in_progress": "bg-amber-50 text-amber-700",
    "completed": "bg-green-50 text-green-700",
    "stale": "bg-red-50 text-red-600",
}

_STATUS_LABELS = {
    "open": "Open",
    "in_progress": "In Progress",
    "completed": "Completed",
    "stale": "Stale",
}


def _current_time() -> str:
    tz = ZoneInfo(settings.aegis_timezone)
    return datetime.now(tz).strftime("%-I:%M %p %Z")


@router.get("")
async def actions_list(
    request: Request,
    q: str = Query("", description="Search by description"),
    status: str = Query("", description="Filter by status"),
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_session),
):
    per_page = 25

    items, total = await get_action_items(
        session,
        status=status if status else None,
        search=q if q else None,
        page=page,
        per_page=per_page,
    )

    # Resolve assignee names
    assignee_ids = [item.assignee_id for item in items if item.assignee_id]
    assignees = await get_persons_by_ids(session, assignee_ids)

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(
        request,
        "actions.html",
        {
            "items": items,
            "assignees": assignees,
            "q": q,
            "status_filter": status,
            "status_options": _STATUS_OPTIONS,
            "status_colors": _STATUS_COLORS,
            "status_labels": _STATUS_LABELS,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "current_time": _current_time(),
        },
    )


@router.post("/{action_id}/status")
async def update_status(
    request: Request,
    action_id: int,
    new_status: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if new_status not in _STATUS_OPTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    await update_action_item_status(session, action_id, new_status)

    # Determine next status for cycling
    current_idx = _STATUS_OPTIONS.index(new_status)
    next_status = _STATUS_OPTIONS[(current_idx + 1) % len(_STATUS_OPTIONS)]

    color = _STATUS_COLORS.get(new_status, "bg-gray-100 text-gray-600")
    label = _STATUS_LABELS.get(new_status, new_status)

    html = (
        f'<form hx-post="/actions/{action_id}/status" hx-swap="outerHTML" hx-target="this">'
        f'<input type="hidden" name="new_status" value="{next_status}">'
        f'<button type="submit" '
        f'class="inline-flex items-center rounded-full px-2 py-1 text-xs font-medium cursor-pointer '
        f'transition-colors hover:opacity-80 {color}">'
        f'{label}</button></form>'
    )
    return HTMLResponse(html)
