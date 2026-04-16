"""Department health — list and detail views with open items and overdue counts."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import Person
from aegis.db.repositories import (
    get_department_by_id,
    get_department_members,
    get_department_open_items,
    get_department_workstreams,
    get_departments,
)
from aegis.intelligence.sentiment import get_department_sentiment
from aegis.web import templates

router = APIRouter()
settings = get_settings()


def _current_time() -> str:
    tz = ZoneInfo(settings.aegis_timezone)
    return datetime.now(tz).strftime("%-I:%M %p %Z")


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


@router.get("/departments")
async def departments_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Department list page with health cards."""
    departments = await get_departments(session)

    # Build card data for each department
    dept_cards = []
    for dept in departments:
        members = await get_department_members(session, dept.id)
        open_items = await get_department_open_items(session, dept.id)

        # Look up head name
        head_name = None
        if dept.head_id:
            head = await session.get(Person, dept.head_id)
            if head:
                head_name = head.name

        # Health status: green/yellow/red
        if open_items["total_overdue"] > 0:
            health = "red"
        elif open_items["total_open"] > 5:
            health = "yellow"
        else:
            health = "green"

        # Fetch sentiment aggregation for this department
        sentiment = await get_department_sentiment(session, dept.id)

        dept_cards.append({
            "department": dept,
            "member_count": len(members),
            "head_name": head_name,
            "open_items": open_items,
            "health": health,
            "sentiment": sentiment,
        })

    # Sort by member count descending
    dept_cards.sort(key=lambda c: c["member_count"], reverse=True)

    return templates.TemplateResponse(
        request,
        "departments.html",
        {
            "dept_cards": dept_cards,
            "total_departments": len(departments),
            "current_time": _current_time(),
        },
    )


@router.get("/departments/{dept_id}")
async def department_detail(
    request: Request,
    dept_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Department detail page with members, open items, and workstreams."""
    dept = await get_department_by_id(session, dept_id)
    if not dept:
        return HTMLResponse(
            '<div class="p-8 text-center text-red-600">Department not found</div>',
            status_code=404,
        )

    members = await get_department_members(session, dept.id)
    open_items = await get_department_open_items(session, dept.id)
    workstreams = await get_department_workstreams(session, dept.id)

    # Look up head name
    head_name = None
    if dept.head_id:
        head = await session.get(Person, dept.head_id)
        if head:
            head_name = head.name

    sentiment = await get_department_sentiment(session, dept.id)
    tz = _local_tz()

    return templates.TemplateResponse(
        request,
        "department_detail.html",
        {
            "dept": dept,
            "members": members,
            "open_items": open_items,
            "workstreams": workstreams,
            "head_name": head_name,
            "sentiment": sentiment,
            "current_time": _current_time(),
            "tz": tz,
        },
    )
