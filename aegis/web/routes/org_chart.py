"""Org Chart — inferred organizational structure visualization."""

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import Department, Person
from aegis.web import templates

router = APIRouter()
settings = get_settings()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


def _build_tree(people: list[Person]) -> dict:
    """Build a tree structure from manager_id relationships.

    Returns a dict with:
      - roots: list of Person objects with no manager (top of hierarchy)
      - children: {person_id: [Person, ...]} mapping
    """
    children: dict[int, list[Person]] = defaultdict(list)
    roots: list[Person] = []

    people_by_id = {p.id: p for p in people}

    for person in people:
        if person.manager_id and person.manager_id in people_by_id:
            children[person.manager_id].append(person)
        else:
            roots.append(person)

    # Sort children by name for consistent display
    for pid in children:
        children[pid].sort(key=lambda p: (p.name or ""))

    roots.sort(key=lambda p: (p.name or ""))

    return {"roots": roots, "children": dict(children)}


@router.get("/org")
async def org_chart(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Org chart page showing hierarchical structure by department."""
    # Load all people with departments
    people_stmt = (
        select(Person)
        .options(joinedload(Person.department), joinedload(Person.manager))
        .where(Person.is_external.is_(False))
        .order_by(Person.name)
    )
    result = await session.execute(people_stmt)
    all_people = list(result.scalars().unique().all())

    # Load departments
    dept_stmt = select(Department).order_by(Department.name)
    dept_result = await session.execute(dept_stmt)
    departments = list(dept_result.scalars().all())

    # Group people by department
    dept_people: dict[int | None, list[Person]] = defaultdict(list)
    for person in all_people:
        dept_people[person.department_id].append(person)

    # Build tree for each department
    dept_trees: list[dict] = []
    for dept in departments:
        people_in_dept = dept_people.get(dept.id, [])
        if not people_in_dept:
            continue
        tree = _build_tree(people_in_dept)
        dept_trees.append({
            "department": dept,
            "people_count": len(people_in_dept),
            "roots": tree["roots"],
            "children": tree["children"],
        })

    # Unassigned people (no department)
    unassigned = dept_people.get(None, [])
    unassigned_tree = _build_tree(unassigned) if unassigned else {"roots": [], "children": {}}

    tz = _local_tz()
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "org_chart.html",
        {
            "dept_trees": dept_trees,
            "unassigned_roots": unassigned_tree["roots"],
            "unassigned_children": unassigned_tree["children"],
            "unassigned_count": len(unassigned),
            "total_people": len(all_people),
            "total_departments": len(departments),
            "current_time": now_local.strftime("%-I:%M %p %Z"),
        },
    )
