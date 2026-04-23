"""Department health — list and detail views with open items and overdue counts."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import Department, Person
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

    # Get all people for assignment dropdown
    from aegis.db.repositories import get_all_people
    all_people = await get_all_people(session)

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
            "all_people": all_people,
        },
    )


@router.post("/departments")
async def create_department(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Create a new department."""
    dept = Department(
        name=name,
        description=description or None,
        source="manual",
        confidence=1.0,
    )
    session.add(dept)
    await session.commit()
    return RedirectResponse(url="/departments", status_code=303)


@router.post("/departments/{dept_id}/edit")
async def edit_department(
    request: Request,
    dept_id: int,
    name: str = Form(...),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Update department name and description."""
    dept = await get_department_by_id(session, dept_id)
    if not dept:
        return HTMLResponse(
            '<div class="p-4 text-red-600">Department not found</div>',
            status_code=404,
        )
    dept.name = name
    dept.description = description or None
    await session.commit()
    return RedirectResponse(url=f"/departments/{dept_id}", status_code=303)


@router.post("/departments/{dept_id}/delete")
async def delete_department(
    request: Request,
    dept_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Delete a department, setting department_id to NULL on affected people."""
    dept = await get_department_by_id(session, dept_id)
    if not dept:
        return HTMLResponse(
            '<div class="p-4 text-red-600">Department not found</div>',
            status_code=404,
        )
    # Unassign people from this department
    await session.execute(
        update(Person).where(Person.department_id == dept_id).values(department_id=None)
    )
    await session.delete(dept)
    await session.commit()
    return RedirectResponse(url="/departments", status_code=303)


@router.post("/departments/{dept_id}/assign")
async def assign_person_to_department(
    request: Request,
    dept_id: int,
    person_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Assign a person to this department."""
    dept = await get_department_by_id(session, dept_id)
    if not dept:
        return HTMLResponse(
            '<div class="p-4 text-red-600">Department not found</div>',
            status_code=404,
        )
    person = await session.get(Person, person_id)
    if not person:
        return HTMLResponse(
            '<div class="p-4 text-red-600">Person not found</div>',
            status_code=404,
        )
    person.department_id = dept_id
    await session.commit()
    return RedirectResponse(url=f"/departments/{dept_id}", status_code=303)
