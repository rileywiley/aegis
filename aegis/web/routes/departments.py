"""Department health — list and detail views with open items and overdue counts."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
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
from aegis.web.breadcrumb import resolve_breadcrumb

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
    from_url: str | None = Query(None, alias="from"),
    session: AsyncSession = Depends(get_session),
):
    """Department detail page with members, open items, and workstreams."""
    back_url, back_label = resolve_breadcrumb(request, from_url, "/departments", "Departments")
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
            "back_url": back_url,
            "back_label": back_label,
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


@router.post("/departments/{dept_id}/remove-person/{person_id}")
async def remove_person_from_department(
    request: Request,
    dept_id: int,
    person_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Remove a person from this department (sets department_id to NULL)."""
    person = await session.get(Person, person_id)
    if person and person.department_id == dept_id:
        person.department_id = None
        await session.commit()
    return RedirectResponse(url=f"/departments/{dept_id}", status_code=303)


@router.get("/departments/{dept_id}/people-search")
async def people_search(
    request: Request,
    dept_id: int,
    q: str = Query(""),
    session: AsyncSession = Depends(get_session),
):
    """HTMX partial: search people by name for department assignment."""
    from sqlalchemy import or_
    if not q or len(q) < 2:
        return HTMLResponse("")
    pattern = f"%{q}%"
    stmt = (
        select(Person)
        .where(or_(Person.name.ilike(pattern), Person.email.ilike(pattern)))
        .order_by(Person.name)
        .limit(10)
    )
    result = await session.execute(stmt)
    people = list(result.scalars().all())
    if not people:
        return HTMLResponse('<div class="p-2 text-xs text-gray-400">No matches</div>')
    html_parts = []
    for p in people:
        html_parts.append(
            f'<form action="/departments/{dept_id}/assign" method="post" class="contents">'
            f'<input type="hidden" name="person_id" value="{p.id}">'
            f'<button type="submit" class="w-full text-left px-3 py-2 text-sm hover:bg-aegis-50 transition-colors">'
            f'{p.name}<span class="text-xs text-gray-400 ml-2">{p.email or ""}</span>'
            f'</button></form>'
        )
    return HTMLResponse(
        '<div class="divide-y divide-gray-100">' + "".join(html_parts) + '</div>'
    )
