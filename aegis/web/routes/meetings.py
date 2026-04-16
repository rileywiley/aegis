"""Meetings routes — list, detail, exclude toggle."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import Meeting, MeetingAttendee
from aegis.db.repositories import get_meeting_attendees, get_meeting_by_id, set_meeting_excluded
from aegis.web import templates

router = APIRouter(prefix="/meetings")
settings = get_settings()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


@router.get("")
async def meetings_list(
    request: Request,
    q: str = Query("", description="Search by title"),
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_session),
):
    per_page = 25
    tz = _local_tz()

    # Base query
    stmt = select(Meeting).order_by(Meeting.start_time.desc())
    if q:
        stmt = stmt.where(Meeting.title.ilike(f"%{q}%"))

    # Count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    # Paginate
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await session.execute(stmt)
    meetings = list(result.scalars().all())

    # Get attendee counts
    attendee_counts: dict[int, int] = {}
    if meetings:
        ids = [m.id for m in meetings]
        cnt_stmt = (
            select(MeetingAttendee.meeting_id, func.count())
            .where(MeetingAttendee.meeting_id.in_(ids))
            .group_by(MeetingAttendee.meeting_id)
        )
        cnt_result = await session.execute(cnt_stmt)
        for mid, cnt in cnt_result:
            attendee_counts[mid] = cnt

    total_pages = max(1, (total + per_page - 1) // per_page)

    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "meetings.html",
        {
            "meetings": meetings,
            "attendee_counts": attendee_counts,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )


@router.get("/{meeting_id}")
async def meeting_detail(
    request: Request,
    meeting_id: int,
    session: AsyncSession = Depends(get_session),
):
    meeting = await get_meeting_by_id(session, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    attendees = await get_meeting_attendees(session, meeting_id)
    tz = _local_tz()
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "meeting": meeting,
            "attendees": attendees,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )


@router.post("/{meeting_id}/exclude")
async def toggle_exclude(
    request: Request,
    meeting_id: int,
    session: AsyncSession = Depends(get_session),
):
    meeting = await get_meeting_by_id(session, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    new_state = not meeting.is_excluded
    await set_meeting_excluded(session, meeting_id, new_state)

    # Return the updated button via HTMX
    label = "Include" if new_state else "Exclude"
    color_cls = "bg-green-600 hover:bg-green-700" if new_state else "bg-red-600 hover:bg-red-700"
    html = (
        f'<button hx-post="/meetings/{meeting_id}/exclude" hx-swap="outerHTML" '
        f'class="{color_cls} text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors">'
        f'{label}</button>'
    )
    return HTMLResponse(html)
