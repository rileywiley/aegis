"""People directory — browse, search, filter, and review person records."""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import Department, Person
from aegis.web import templates

router = APIRouter()
settings = get_settings()

PER_PAGE = 25

VALID_SORT_COLS = {
    "name": Person.name,
    "email": Person.email,
    "title": Person.title,
    "seniority": Person.seniority,
    "interaction_count": Person.interaction_count,
    "last_seen": Person.last_seen,
}


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


@router.get("/people")
async def people_directory(
    request: Request,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    department: int | None = None,
    seniority: str | None = None,
    needs_review: bool | None = None,
    is_external: bool | None = None,
    sort: str = "name",
    order: str = "asc",
    page: int = Query(1, ge=1),
):
    """People directory with search, filter, sort, and pagination."""
    # Base query
    stmt = select(Person)
    count_stmt = select(func.count()).select_from(Person)

    # Apply search filter
    if q:
        search_filter = or_(
            Person.name.ilike(f"%{q}%"),
            Person.email.ilike(f"%{q}%"),
        )
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    # Apply filters
    if department is not None:
        stmt = stmt.where(Person.department_id == department)
        count_stmt = count_stmt.where(Person.department_id == department)
    if seniority:
        stmt = stmt.where(Person.seniority == seniority)
        count_stmt = count_stmt.where(Person.seniority == seniority)
    if needs_review is not None:
        stmt = stmt.where(Person.needs_review == needs_review)
        count_stmt = count_stmt.where(Person.needs_review == needs_review)
    if is_external is not None:
        stmt = stmt.where(Person.is_external == is_external)
        count_stmt = count_stmt.where(Person.is_external == is_external)

    # Sorting
    sort_col = VALID_SORT_COLS.get(sort, Person.name)
    if order == "desc":
        stmt = stmt.order_by(sort_col.desc())
    else:
        stmt = stmt.order_by(sort_col.asc())

    # Get total count
    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()
    total_pages = max(1, math.ceil(total / PER_PAGE))

    # Pagination
    stmt = stmt.offset((page - 1) * PER_PAGE).limit(PER_PAGE)

    # Eager load department relationship
    stmt = stmt.options(joinedload(Person.department))

    result = await session.execute(stmt)
    people = list(result.scalars().unique().all())

    # Get all departments for filter dropdown
    dept_stmt = select(Department).order_by(Department.name)
    dept_result = await session.execute(dept_stmt)
    departments = list(dept_result.scalars().all())

    # Count needs-review
    review_count_stmt = select(func.count()).select_from(Person).where(Person.needs_review.is_(True))
    review_count_result = await session.execute(review_count_stmt)
    review_count = review_count_result.scalar_one()

    tz = _local_tz()
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "people.html",
        {
            "people": people,
            "total": total,
            "total_pages": total_pages,
            "page": page,
            "q": q,
            "department_filter": department,
            "seniority_filter": seniority,
            "needs_review_filter": needs_review,
            "is_external_filter": is_external,
            "sort": sort,
            "order": order,
            "departments": departments,
            "review_count": review_count,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )


@router.get("/people/review", response_class=HTMLResponse)
async def people_review(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """HTMX partial: needs-review queue."""
    stmt = (
        select(Person)
        .where(Person.needs_review.is_(True))
        .options(joinedload(Person.department))
        .order_by(Person.last_seen.desc())
    )
    result = await session.execute(stmt)
    people = list(result.scalars().unique().all())

    tz = _local_tz()

    return templates.TemplateResponse(
        request,
        "components/people_review.html",
        {
            "people": people,
            "tz": tz,
        },
    )


@router.post("/people/{person_id}/approve", response_class=HTMLResponse)
async def approve_person(
    request: Request,
    person_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Apply LLM suggestion and mark as reviewed."""
    person = await session.get(Person, person_id)
    if not person:
        return HTMLResponse('<div class="text-red-600 text-sm">Person not found</div>')

    # Apply LLM suggestion fields if present
    suggestion = person.llm_suggestion or {}
    if suggestion.get("title"):
        person.title = suggestion["title"]
    if suggestion.get("role"):
        person.role = suggestion["role"]
    if suggestion.get("seniority"):
        person.seniority = suggestion["seniority"]
    if suggestion.get("department"):
        person.org = suggestion["department"]

    person.needs_review = False
    await session.commit()

    return HTMLResponse(
        f'<div id="review-card-{person_id}" class="rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-700">'
        f"Approved: {person.name}</div>"
    )


@router.post("/people/{person_id}/dismiss", response_class=HTMLResponse)
async def dismiss_person(
    request: Request,
    person_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Mark as reviewed without applying suggestion."""
    stmt = (
        update(Person)
        .where(Person.id == person_id)
        .values(needs_review=False)
    )
    await session.execute(stmt)
    await session.commit()

    return HTMLResponse(
        f'<div id="review-card-{person_id}" class="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-500">'
        f"Dismissed</div>"
    )


@router.get("/people/{person_id}/edit-form", response_class=HTMLResponse)
async def edit_person_form(
    request: Request,
    person_id: int,
    session: AsyncSession = Depends(get_session),
):
    """HTMX partial: inline edit form for a person in the review queue."""
    person = await session.get(Person, person_id)
    if not person:
        return HTMLResponse('<div class="text-red-600 text-sm">Person not found</div>')

    dept_stmt = select(Department).order_by(Department.name)
    dept_result = await session.execute(dept_stmt)
    departments = list(dept_result.scalars().all())

    return templates.TemplateResponse(
        request,
        "components/people_edit_form.html",
        {
            "person": person,
            "departments": departments,
        },
    )


@router.post("/people/{person_id}/edit", response_class=HTMLResponse)
async def edit_person(
    request: Request,
    person_id: int,
    session: AsyncSession = Depends(get_session),
    name: str = Form(...),
    title: str = Form(""),
    role: str = Form(""),
    seniority: str = Form("unknown"),
    department_id: str = Form(""),
):
    """Apply manual edits and mark as reviewed."""
    person = await session.get(Person, person_id)
    if not person:
        return HTMLResponse('<div class="text-red-600 text-sm">Person not found</div>')

    person.name = name.strip()
    person.title = title.strip() or None
    person.role = role.strip() or None
    if seniority in ("executive", "senior", "mid", "junior", "unknown"):
        person.seniority = seniority
    if department_id and department_id.isdigit():
        person.department_id = int(department_id)
    else:
        person.department_id = None
    person.needs_review = False

    await session.commit()

    return HTMLResponse(
        f'<div id="review-card-{person_id}" class="rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-700">'
        f"Updated: {person.name}</div>"
    )
